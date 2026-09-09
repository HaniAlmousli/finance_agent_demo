"""
langgraph_bedrock.py — the finance_app agent, deployable to AgentCore Runtime
==============================================================================

The LangGraph agent from the tutorial notebook (three tools: `get_weather`,
`get_transactions`, `calculator`), adapted to the finance_app architecture.
The tool bodies live in tools_core.py, shared verbatim
with the gateway Lambda so local and gateway mode can never diverge.

Entitlement model (MUST match config.yaml — scopes are per-TIER, not per-tool):

    group                 -> TIER scope in the JWT             -> tools
    ---------------------------------------------------------------------------
    finance-agent-users   -> finance-gateway/basic.invoke      -> get_weather,
                                                                  get_transactions
    premium-users         -> finance-gateway/premium.invoke    -> calculator,
                                                                  convert_currency
    (+ requires finance-agent-users — premium alone grants nothing)

    Sarah  = both groups            -> basic + premium -> all tools
    John   = finance-agent-users    -> basic only      -> get_weather, get_transactions

    get_transactions adds a SECOND axis: row-level security. Both users hold
    the tool, but each only ever sees their own rows — the user identity is
    resolved from the verified token by the runtime (never by the model),
    so "show me John's transactions" as Sarah has no parameter to exploit.

How the entitlement reaches this code: finance_app.py's /chat verifies the
user's Cognito JWT and forwards it inside the payload as `access_token`; this
agent VERIFIES that token's signature again (see _verify_token) before
trusting any of its claims, because the Runtime is directly reachable and a
caller who bypasses finance_app could otherwise forge groups/scopes in the
payload. The token's `scope` claim names the TIERS the user may enter;
TIER_TOOLS below maps tiers to tools in local mode. A tool outside the user's
tiers never exists for the model at all (it can't be prompt-injected into
calling it). Adding a tool = add it to a tier here (+ gateway target at M3);
Cognito is never touched.

Mode (gateway vs local), the model, and the pool identity are read from the
container's ENVIRONMENT — set by infra.py deploy_agent — never from the
payload. That is what stops a direct Runtime caller from flipping the
enforcement mode, swapping the model, or pointing the agent at arbitrary MCP
endpoints. In gateway mode the same tier scopes are enforced by each
gateway's allowedScopes — the rule never changes, only where it runs.

Same trust-boundary logic applies to the SESSION id: the Runtime routes by a
client-set session header it never binds to the token, so the entrypoint
rejects any session id that isn't this user's own finance-<sub> (see the
session-identity check). This keeps a direct caller from targeting another
subject's session — critical NOW that per-session conversation memory exists
(AGENT_SESSION_MEMORY): the thread is the thing that binding protects.

Deploy (from this directory, per the notebook flow):

    from bedrock_agentcore_starter_toolkit import Runtime
    rt = Runtime()
    rt.configure(entrypoint="langgraph_bedrock.py",
                 auto_create_execution_role=True, auto_create_ecr=True,
                 requirements_file="requirements.txt",
                 region="us-east-1", agent_name="demo_bedrock_agentcore_finance_agent")
    rt.launch()
"""

import json
import os

import jwt  # PyJWT — now signature-verifying, see _verify_token
from jwt import PyJWKClient
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from langchain_core.messages import HumanMessage, SystemMessage, trim_messages
from langchain_core.tools import StructuredTool, tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition

import tools_core  # shared tool bodies (also shipped into the gateway Lambda)

app = BedrockAgentCoreApp()


# ---------------------------------------------------------------------------
# Deploy-time configuration — read from the container's ENVIRONMENT, never
# from the request payload. infra.py deploy_agent sets these (from config.yaml
# + infra_outputs.json) when it launches the Runtime. Keeping MODE, model, and
# pool identity here — not in the payload — means a caller who reaches the
# Runtime directly cannot flip gateway/local mode, swap the model, or point the
# agent at arbitrary MCP endpoints.
# ---------------------------------------------------------------------------
REGION = os.environ.get("REGION", "")  # from config.yaml via deploy_agent env
POOL_ID = os.environ.get("POOL_ID", "")
CLIENT_ID = os.environ.get("CLIENT_ID", "")
AGENT_ADMISSION_GROUP = os.environ.get("AGENT_ADMISSION_GROUP", "finance-agent-users")
GATEWAY_URLS = json.loads(os.environ.get("GATEWAY_URLS", "{}"))  # {} => local mode
AGENT_LLM = json.loads(os.environ.get("AGENT_LLM", "{}"))
SESSION_MEMORY = os.environ.get("AGENT_SESSION_MEMORY", "").lower() == "true"

# ---------------------------------------------------------------------------
# Per-SESSION conversation memory (enabled by config.yaml agent.session_memory,
# baked into the container env by deploy-agent — like mode and model, the
# caller can never flip it). The checkpointer is a MODULE GLOBAL: it lives in
# this microVM's process memory, which the Runtime keeps warm for the session
# — so history survives across /chat calls and dies with the microVM
# (~15 min idle, 8 h cap, any redeploy, or an explicit StopRuntimeSession —
# invoke_direct.py --stop doubles as "clear my conversation").
#
# State is keyed by thread_id = finance-<VERIFIED sub> (never the raw session
# header): a caller cannot name someone else's thread, and each microVM is
# per-session anyway, so its checkpointer only ever holds ONE user's thread.
# ---------------------------------------------------------------------------
_CHECKPOINTER = MemorySaver()

# Bound what each model call REPLAYS (input tokens grow with history — the
# stored thread may grow; the wire payload must not). Counted in MESSAGES
# (token_counter=len), ~12 turns.
MAX_HISTORY_MESSAGES = 24

# Upper bound on a single user prompt — a full page or so of text. The Runtime
# is directly reachable, so this guards against oversized input (token cost /
# context abuse). Mirror this in finance_app.py's ChatRequest and static/
# index.html's maxlength; keep the three in sync.
MAX_PROMPT_CHARS = 4000

ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
# Cached pool public keys — one HTTPS GET on first use, then in-memory. Same
# mechanism finance_app.py uses; PyJWKClient re-fetches automatically on key
# rotation (an unknown `kid`). Construction here does no network I/O.
_jwks = PyJWKClient(f"{ISSUER}/.well-known/jwks.json")


def _verify_token(token: str) -> dict:
    """Cryptographically verify the user's Cognito access token, mirroring
    finance_app.py's current_user(). The Runtime's customJWTAuthorizer only
    validates the Authorization HEADER; the token inside the PAYLOAD — the one
    every entitlement decision below reads — must be verified here, or a direct
    caller could forge its groups/scopes. Raises on any failure (bad signature,
    wrong issuer, expired, wrong kind); the caller turns that into a denial."""
    key = _jwks.get_signing_key_from_jwt(token)
    claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=ISSUER)
    # token_use rejects an ID token replayed as an access token; client_id
    # (when configured) rejects a valid token from a different app client.
    if claims.get("token_use") != "access":
        raise jwt.InvalidTokenError("not an access token")
    if CLIENT_ID and claims.get("client_id") != CLIENT_ID:
        raise jwt.InvalidTokenError("token not issued for this app client")
    return claims


# ---------------------------------------------------------------------------
# The three tools. The BODIES live in tools_core (shared with the gateway
# Lambda so the two can never drift); here each is wrapped in a LangChain
# @tool whose NAME + docstring become the schema the model sees. The tool
# names match config.yaml's gateway tiers and the Lambda's DISPATCH keys, so
# local and gateway mode expose an identical tool surface (the `weather` vs
# `get_weather` divergence this refactor fixed).
# ---------------------------------------------------------------------------
@tool
def calculator(expression: str) -> str:
    """
    Calculate the result of a mathematical expression.

    Args:
        expression: A mathematical expression as a string
                    (e.g., "2 + 3 * 4", "sqrt(16)", "sin(pi/2)")

    Returns:
        The result of the calculation as a string
    """
    return tools_core.calculator(expression)


@tool("get_weather")
def get_weather(country: str) -> str:
    """
    Get the current weather in a country.

    Args:
        country: The country to get the weather for, e.g. "USA", "Jordan"

    Returns:
        The current weather in that country
    """
    return tools_core.get_weather(country)


def _local_get_transactions(sub: str):
    """Local-mode get_transactions, bound to the VERIFIED caller.

    Built per request (not module-level like the other tools) because it
    closes over the sub from the entrypoint's signature-verified claims —
    the model-visible schema has no identity parameter at all, mirroring
    gateway mode's hide-and-inject."""
    @tool("get_transactions")
    def get_transactions(start_date: str = "", end_date: str = "") -> str:
        """
        Look up the user's own bank transactions. Use when the user asks
        about their transactions, spending, purchases, or income.

        Args:
            start_date: Optional range start, YYYY-MM-DD
            end_date: Optional range end (inclusive), YYYY-MM-DD

        Returns:
            The user's matching transactions, newest first
        """
        return tools_core.get_transactions(sub, start_date, end_date)
    return get_transactions


# ---------------------------------------------------------------------------
# Entitlement (LOCAL mode): TIER scope -> tools in that tier.
# Mirrors the gateway-mode tool→tier assignment in config.yaml gateways.tiers
# — keep the two in sync. Adding a tool = one entry in a tier list here;
# Cognito scopes never change.
# ---------------------------------------------------------------------------
TIER_TOOLS = {
    "finance-gateway/basic.invoke": [get_weather],
    "finance-gateway/premium.invoke": [calculator],
}

# Identity-bound tools (row-level security) are FACTORIES, not tool objects:
# they're built per request in the entrypoint, closing over the verified sub.
TIER_IDENTITY_TOOLS = {
    "finance-gateway/basic.invoke": [_local_get_transactions],
}


# ---------------------------------------------------------------------------
# Agent construction — same manual LangGraph build as the notebook, but the
# tool list is a parameter (it varies per user), and a user with tool-less
# scopes still gets a working plain chatbot.
# ---------------------------------------------------------------------------
# Fallback LLM settings — used only if the AGENT_LLM env var is unset (e.g.
# direct invocation for testing). The real source of truth is config.yaml's
# `agent:` block, baked into the container's environment by infra.py
# deploy_agent. Haiku = cheapest current tool-calling Claude tier.
DEFAULT_LLM = {
    "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "temperature": 0.1,
}


# Friendly one-liners for the system prompt, keyed by TOOL NAME so they work
# for BOTH local @tool objects and gateway MCP tool objects — their identities
# differ (a KeyError waiting to happen if keyed by object), but their names are
# reconciled across modes. Any tool without a blurb falls back to its own
# description, so a new/unknown tool degrades gracefully instead of crashing.
_CAPABILITY_BLURB = {
    "calculator": "do simple math calculations",
    "get_weather": "tell the weather in a country",
    "get_transactions": "look up the user's own recent transactions",
}


def _capability_blurb(t) -> str:
    return _CAPABILITY_BLURB.get(t.name) or (t.description or t.name)


def create_agent(tools, llm_cfg=None):
    """Create the LangGraph agent bound to exactly `tools`."""
    from langchain_aws import ChatBedrock

    cfg = {**DEFAULT_LLM, **(llm_cfg or {})}
    llm = ChatBedrock(
        model_id=cfg["model_id"],
        model_kwargs={"temperature": cfg["temperature"]},
        region_name=REGION or None,   # explicit; falls back to ambient if unset
    )
    llm_with_tools = llm.bind_tools(tools) if tools else llm

    # Keyed by tool NAME, not object — the gateway-mode tools are MCP wrappers,
    # not the local @tool objects, so an identity-keyed lookup would KeyError.
    capabilities = " and ".join(_capability_blurb(t) for t in tools) \
        or "chat (no tools are enabled for this user)"
    # The "[TOOL: ...]" instruction makes tool provenance visible in the demo:
    # tool results carry a tag, and the model must quote it — so a reply
    # WITHOUT the tag proves the answer came from the LLM itself (e.g. John
    # doing math with no calculator), and "@lambda" in the tag proves gateway
    # mode. Without this instruction the model tends to paraphrase the tag away.
    #
    # Rule (2) covers EXTERNAL tools (OpenAPI targets like convert_currency):
    # their results are raw vendor API data with no tag of ours, so the model
    # adds one itself. Know the difference in evidence strength: rules (1)'s
    # tags are DATA-CARRIED (they flow from the tool result); rule (2)'s are
    # MODEL-ASSERTED (the model's own claim) — demo-grade only. The ground
    # truth for "did a tool really run?" is always the GenAI Observability
    # trace, never the badge.
    system_message = (
        f"You're a helpful finance assistant. You can {capabilities}. "
        "Provenance tagging rules: "
        "(1) When a tool result contains a tag like [TOOL: ...], you MUST "
        "include that tag verbatim at the start of your answer. "
        "(2) If you used a tool whose result contains no such tag, start "
        "your answer with [TOOL: <tool name>] yourself. "
        "(3) If you did not use any tool, never add a tag."
    )

    def chatbot(state: MessagesState):
        messages = state["messages"]
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=system_message)] + messages
        # With session memory the state accumulates across /chat calls —
        # bound what this CALL replays. start_on="human" is load-bearing:
        # a trim must never split an assistant tool_use from its
        # tool_result (Bedrock rejects orphaned pairs).
        messages = trim_messages(messages, strategy="last",
                                 token_counter=len,
                                 max_tokens=MAX_HISTORY_MESSAGES,
                                 start_on="human", include_system=True)
        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    graph_builder = StateGraph(MessagesState)
    graph_builder.add_node("chatbot", chatbot)
    if tools:
        graph_builder.add_node("tools", ToolNode(tools))
        graph_builder.add_conditional_edges("chatbot", tools_condition)
        graph_builder.add_edge("tools", "chatbot")
    graph_builder.set_entry_point("chatbot")
    # The graph is rebuilt per request (tools are per-user, token-bound), but
    # the CHECKPOINTER is shared — state is keyed by thread_id, so a fresh
    # compile resumes the same conversation.
    return graph_builder.compile(
        checkpointer=_CHECKPOINTER if SESSION_MEMORY else None)


# ---------------------------------------------------------------------------
# Gateway mode (M3): load this user's tools FROM the AgentCore Gateways
# ---------------------------------------------------------------------------
def _is_auth_rejection(exc: Exception) -> bool:
    """True only for the EXPECTED insufficient-scope rejection (401/403).

    A basic user probing the premium gateway is normal operation — the tier
    is simply unavailable to them. Anything else (timeout, DNS, malformed
    schema, 5xx) is a real failure and must propagate, not masquerade as
    'not entitled'.
    """
    # httpx.HTTPStatusError (raised by the MCP client's transport) carries
    # the response; other libs put the code on the exception itself.
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in (401, 403):
        return True
    # ExceptionGroup (TaskGroup/anyio): auth-rejection iff EVERY leaf is one.
    subs = getattr(exc, "exceptions", None)
    if subs:
        return all(_is_auth_rejection(s) for s in subs)
    return False


def _bind_identity(t, token: str):
    """Hide `access_token` from the MODEL; inject the VERIFIED one at call time.

    Row-level-secured gateway tools (e.g. get_transactions) declare an
    access_token parameter in their gateway schema — the Lambda needs it to
    resolve the caller's identity via cognito get_user. The model must never
    see or control it: rebuild the model-visible schema WITHOUT the field and
    add the entrypoint's signature-verified token when the call happens. A
    prompt-injected "show me John's transactions" has no parameter to aim at.
    Tools without the parameter pass through untouched."""
    props = dict(t.args or {})
    if "access_token" not in props:
        return t
    props.pop("access_token")
    schema = getattr(t, "args_schema", None)
    if isinstance(schema, dict):
        new_schema = {**schema, "properties": props,
                      "required": [r for r in schema.get("required", [])
                                   if r != "access_token"]}
    else:
        new_schema = {"type": "object", "properties": props}

    async def _run(**model_args):
        return await t.ainvoke({**model_args, "access_token": token})

    return StructuredTool(name=t.name, description=t.description or "",
                          coroutine=_run, args_schema=new_schema)


async def load_gateway_tools(gateway_urls: dict, token: str) -> list:
    """
    Connect to each tier gateway as an MCP server, presenting the USER'S
    token. Each gateway independently verifies the signature and admits by
    its required TIER scope (allowedScopes), so the returned list is already
    entitlement-filtered:
        Sarah (basic.invoke + premium.invoke) -> weather, transactions, calculator, currency
        John  (basic.invoke only)             -> weather, transactions

    Each gateway is loaded INDEPENDENTLY: John's expected 401/403 from the
    premium tier must not prevent his basic tools from loading. Only that
    auth rejection is treated as "tier unavailable" — unexpected errors
    (timeouts, schema errors, 5xx) propagate and fail the request loudly.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    tools = []
    for tier_name, url in gateway_urls.items():
        client = MultiServerMCPClient({
            tier_name: {
                "transport": "streamable_http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            },
        })
        try:
            tools.extend(_bind_identity(t, token)
                         for t in await client.get_tools())
        except Exception as exc:  # noqa: BLE001 — filtered just below
            if not _is_auth_rejection(exc):
                raise
            # expected: this user's token lacks the tier's required scope
    return tools


# ---------------------------------------------------------------------------
# Entrypoint — runs inside the user's microVM on every invocation
# ---------------------------------------------------------------------------
@app.entrypoint
async def langgraph_bedrock(payload, context):
    """
    payload = {
        "prompt":       <user message>,
        "access_token": <user's Cognito JWT>,   # signature-verified below
    }
    context = the Runtime's RequestContext; context.session_id is the
    X-Amzn-Bedrock-AgentCore-Runtime-Session-Id header (see the session check
    below). The `context` parameter MUST be named exactly that — the SDK only
    passes it when the second arg is literally `context`.

    finance_app.py builds the payload after verifying the token; the agent
    verifies it AGAIN here (defense in depth — the Runtime is directly
    reachable). MODE (gateway vs local), the model, and the pool identity come
    from the container's ENVIRONMENT (set by infra.py deploy_agent), NOT the
    payload — so a direct caller cannot flip mode, swap the model, or inject
    MCP URLs.
    """
    # The Runtime is directly reachable, so the payload is UNTRUSTED input —
    # FastAPI's ChatRequest validates finance_app's /chat route, not this
    # entrypoint. Assert the shapes we depend on before touching them.
    if not isinstance(payload, dict):
        return "Bad request: payload must be a JSON object."

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        return "Access denied: no credential presented."

    # Verify the payload token's SIGNATURE (not just decode it): this is what
    # lets us trust cognito:groups and scope below even from a caller who
    # bypassed finance_app and hand-crafted the payload.
    try:
        claims = _verify_token(token)
    except Exception as exc:  # noqa: BLE001 — any verification failure = deny
        return f"Access denied: invalid token ({exc})."

    # Hardening gate: even a valid pool token is refused unless the user is
    # in the agent's admission group. Both Sarah and John pass this.
    if AGENT_ADMISSION_GROUP not in claims.get("cognito:groups", []):
        return "Access denied: your account is not entitled to the finance agent."

    # ── Session-identity binding (defence in depth) ──────────────────────
    # The Runtime routes by the X-Amzn-Bedrock-AgentCore-Runtime-Session-Id
    # header. finance_app derives it from the VERIFIED sub (finance-<sub>),
    # but a caller reaching the Runtime DIRECTLY sets it freely, and the
    # Runtime's JWT authorizer never checks that the session belongs to the
    # token's subject. Bind it here: reject any session id that isn't THIS
    # user's own, so a caller can't target another subject's session (with
    # session memory enabled, that would mean reading or poisoning that
    # user's conversation thread). The key is the verified sub, never the
    # header. session_id is None only for direct/local invocation with no
    # header — nothing to cross-target then, so allow it.
    expected_session = f"finance-{claims['sub']}"
    caller_session = getattr(context, "session_id", None)
    if caller_session is not None and caller_session != expected_session:
        return "Access denied: session does not belong to this identity."

    if GATEWAY_URLS:
        # ── GATEWAY MODE (M3) ─── selected by the ENVIRONMENT, not the caller
        # Entitlement enforced BY THE GATEWAYS: each tier's allowedScopes
        # admits or rejects the token at tools/list, and re-checks it on
        # every tools/call. The local TIER_TOOLS table is NOT used — same
        # tier rule, moved to a stronger AWS-side enforcement point. Local
        # @tool implementations above are dormant in this mode
        # (gateway_tools_lambda.py runs instead).
        tools = await load_gateway_tools(GATEWAY_URLS, token)
    else:
        # ── LOCAL MODE (gateways disabled) ────────────────────────────
        # THE entitlement step: bind the tools of every TIER the user's
        # scopes admit.
        #   Sarah (basic + premium): weather + transactions + calculator
        #   John  (basic only):      weather + transactions
        # A tool outside the user's tiers never appears in the model's
        # world at all. Identity-bound tools are built HERE, per request,
        # closing over the verified sub (row-level security, local flavor).
        scopes = set(claims.get("scope", "").split())
        tools = [t for tier, ts in TIER_TOOLS.items() if tier in scopes
                 for t in ts]
        tools += [factory(claims["sub"])
                  for tier, fs in TIER_IDENTITY_TOOLS.items() if tier in scopes
                  for factory in fs]

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return "Bad request: 'prompt' must be a non-empty string."
    if len(prompt) > MAX_PROMPT_CHARS:
        return f"Bad request: prompt too long (max {MAX_PROMPT_CHARS} characters)."

    agent = create_agent(tools, llm_cfg=AGENT_LLM or None)
    # Session memory: name the conversation thread by the VERIFIED identity
    # (finance-<sub> from the signature-checked claims — never the raw
    # session header). LangGraph loads that thread's history, appends this
    # turn, and persists the result in the module-level checkpointer.
    invoke_config = ({"configurable": {"thread_id": expected_session}}
                     if SESSION_MEMORY else None)
    response = await agent.ainvoke(
        {"messages": [HumanMessage(content=prompt)]},
        config=invoke_config,
    )
    return response["messages"][-1].content


if __name__ == "__main__":
    app.run()
