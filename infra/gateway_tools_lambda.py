"""
gateway_tools_lambda.py — the backend for ALL gateway tools
============================================================

One Lambda serves every tool behind both gateway tiers (a Lambda target's
toolSchema is a LIST — you do not need one function per tool). The gateway
tells us which tool was called via the client context; we dispatch on it.

Tool bodies are imported from tools_core.py — the SAME module langgraph_bedrock.py
wraps for local mode — so the two environments can never drift. infra.py ships
tools_core.py (and the files listed in config's tools_lambda.include, e.g. the
S9-generated transactions.csv) inside this Lambda's zip: the Lambda carries no
third-party packages, so everything travels with the code. boto3 is the one
exception — preinstalled in every Lambda Python runtime — and is used here ONLY
to resolve caller identity for row-level-secured tools.

Which tools appear on which gateway (the entitlement split) is NOT decided
here — it's decided by which tool schemas infra.py attaches to which tier
(config.yaml: gateways.tiers[].tools). This function will happily run any
tool it knows; the gateway only routes calls the caller's scope admitted.

Deployed automatically by `python infra.py create` when gateways.enabled.
"""

import base64
import json
import os

import boto3        # preinstalled in the Lambda runtime — identity resolution only
import tools_core   # shared tool bodies (shipped into this zip by infra.py)

_cognito = boto3.client("cognito-idp")   # region = the Lambda's own (AWS_REGION)
POOL_ID = os.environ.get("POOL_ID", "")  # set by infra.py at deploy


def _caller_sub(access_token: str):
    """Resolve the VERIFIED identity behind a row-level-secured tool call.

    The gateway does not forward caller identity to Lambda targets, so the
    agent runtime injects the user's access token as a hidden argument and we
    ask Cognito itself: get_user succeeds only for a valid, unexpired,
    UNREVOKED token (stronger than local signature math) and is authorized by
    the token — no IAM permissions needed. Returns the caller's sub, or None
    on any failure. The token is never logged and never appears in results.
    """
    try:
        resp = _cognito.get_user(AccessToken=access_token)
    except Exception:  # noqa: BLE001 — any failure means "not you": deny
        return None
    sub = next((a["Value"] for a in resp["UserAttributes"]
                if a["Name"] == "sub"), None)
    # Cross-pool hardening: get_user proves the token is valid for SOME pool
    # in this region; require OURS. Payload decode only — validity is already
    # proven above, we just read the iss claim.
    if sub and POOL_ID:
        try:
            payload = json.loads(base64.urlsafe_b64decode(
                access_token.split(".")[1] + "=="))
            if not payload.get("iss", "").endswith(POOL_ID):
                return None
        except Exception:  # noqa: BLE001
            return None
    return sub


# ---------------------------------------------------------------------------
# Tool adapters — event -> tools_core call. The bodies are shared verbatim
# with langgraph_bedrock.py (local mode); these wrappers only unpack the
# gateway's event into arguments and tag the result " @lambda" so the demo can
# see the tool ran here in the Lambda rather than inside the agent container.
# ---------------------------------------------------------------------------
def tool_weather(event: dict) -> str:
    return tools_core.get_weather(event.get("country", ""), where=" @lambda")


def tool_calculator(event: dict) -> str:
    return tools_core.calculator(event.get("expression", ""), where=" @lambda")


def tool_get_transactions(event: dict) -> str:
    token = event.get("access_token") or ""
    if not token:
        return "Access denied: no credential presented."
    sub = _caller_sub(token)
    if not sub:
        return "Access denied: invalid or expired credential."
    # user_id is OUR resolved sub — never anything the caller placed in the
    # event — so each caller can only ever read their own rows.
    return tools_core.get_transactions(
        sub, event.get("start_date", ""), event.get("end_date", ""),
        where=" @lambda")


DISPATCH = {
    "get_weather": tool_weather,
    "get_transactions": tool_get_transactions,
    "calculator": tool_calculator,
}


# ---------------------------------------------------------------------------
# Gateway entrypoint
# ---------------------------------------------------------------------------
def handler(event, context):
    """
    The gateway invokes this with:
      event   = the tool's input arguments (per the target's inputSchema)
      context.client_context.custom["bedrockAgentCoreToolName"]
              = "<target-name>___<tool-name>"
    """
    raw = ""
    if context.client_context and context.client_context.custom:
        raw = context.client_context.custom.get("bedrockAgentCoreToolName", "")
    tool_name = raw.split("___")[-1]   # strip the target-name prefix

    fn = DISPATCH.get(tool_name)
    if fn is None:
        return {"error": f"unknown tool '{tool_name}'"}
    return {"result": fn(event or {})}
