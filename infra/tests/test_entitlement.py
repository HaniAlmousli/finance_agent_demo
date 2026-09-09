"""
Local unit tests for the entitlement logic — no AWS required.

Covers the locally-testable subset of the required test matrix:
  1  John receives only basic.invoke                (lambda logic)
  2  Sarah receives both scopes                     (lambda logic)
  3  user in no groups -> no scopes + 403 at /chat
  4  premium-only user -> NO premium.invoke + 403 at /chat
  6  John's model-visible tools exclude the calculator (local tier filter)
  12 correct client but missing scope -> tier yields nothing
  (5, 7-11, 13's runtime/gateway legs live in tests/smoke_test.md — they
   need deployed infra.)

Run:  uv run python -m pytest tests/ -q
"""

import ast
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))

CFG = yaml.safe_load((HERE / "config.yaml").read_text())

BASIC = "finance-gateway/basic.invoke"
PREMIUM = "finance-gateway/premium.invoke"


# ---------------------------------------------------------------------------
# The pre-token Lambda: reproduce infra.py's GENERATED handler byte-for-byte
# semantics, driven by the real config.yaml groups mapping.
# ---------------------------------------------------------------------------
def mint_scopes(user_groups):
    groups = set(user_groups)
    scopes = set()
    for group, spec in CFG["groups"].items():
        if group not in groups:
            continue
        if not set(spec.get("requires", [])) <= groups:
            continue
        scopes.update(spec.get("scopes", []))
    return sorted(scopes)


def test_1_john_gets_basic_only():
    scopes = mint_scopes(["finance-agent-users"])
    assert BASIC in scopes
    assert PREMIUM not in scopes


def test_2_sarah_gets_both():
    assert mint_scopes(["finance-agent-users", "premium-users"]) == \
        sorted([BASIC, PREMIUM])


def test_3_no_groups_no_scopes():
    assert mint_scopes([]) == []


def test_4_premium_only_gets_nothing():
    # premium is an add-on entitlement, not a substitute for base membership
    assert mint_scopes(["premium-users"]) == []


def test_unknown_group_is_inert():
    assert mint_scopes(["hr-users"]) == []
    assert mint_scopes(["finance-agent-users", "hr-users"]) == [BASIC]


# ---------------------------------------------------------------------------
# /chat admission gate (groups-based) — same expression as finance_app.py
# ---------------------------------------------------------------------------
def admitted(claims):
    return CFG["agent_admission_group"] in claims.get("cognito:groups", [])


def test_3b_no_groups_403():
    assert not admitted({"cognito:groups": []})


def test_4b_premium_only_403():
    assert not admitted({"cognito:groups": ["premium-users"]})


def test_john_and_sarah_admitted():
    assert admitted({"cognito:groups": ["finance-agent-users"]})
    assert admitted({"cognito:groups": ["finance-agent-users", "premium-users"]})


# ---------------------------------------------------------------------------
# Local-mode tier filter (langgraph_bedrock.TIER_TOOLS shape) — validated
# against config.yaml's gateway tool assignment to catch drift.
# ---------------------------------------------------------------------------
def tier_tools_from_config():
    return {
        tier["required_scope"]: [t["name"] for t in tier.get("tools", [])]
        for tier in CFG["gateways"]["tiers"]
    }


def tools_for(scope_claim: str):
    scopes = set(scope_claim.split())
    table = tier_tools_from_config()
    return [t for tier, ts in table.items() if tier in scopes for t in ts]


def test_6_john_sees_no_calculator():
    assert "calculator" not in tools_for(BASIC)
    assert set(tools_for(BASIC)) == {"get_weather", "get_transactions"}


def test_sarah_sees_all_three():
    assert set(tools_for(f"{BASIC} {PREMIUM}")) == \
        {"get_weather", "get_transactions", "calculator"}


def test_12_scopeless_token_yields_nothing():
    assert tools_for("") == []


# ---------------------------------------------------------------------------
# Local vs gateway tool-NAME parity. The bug this guards against: the local
# @tool was named `weather` while the gateway/config tool was `get_weather`,
# so the model saw different tool names in each mode — and no test caught it,
# because the tier filter above reads names only from config. We extract the
# local @tool names straight from langgraph_bedrock.py's source with `ast`
# (importing it would need langchain, absent from this venv) and assert they
# equal the config tier names. LangChain's rule: @tool("x") overrides the name,
# otherwise the function name is the tool name.
# ---------------------------------------------------------------------------
def local_tool_names():
    src = (HERE.parent / "agent" / "langgraph_bedrock.py").read_text()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) \
                    and dec.func.id == "tool":
                # @tool("name") — explicit override
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    names.add(dec.args[0].value)
                else:
                    names.add(node.name)
            elif isinstance(dec, ast.Name) and dec.id == "tool":
                # bare @tool — function name is the tool name
                names.add(node.name)
    return names


def test_local_tool_names_match_config():
    config_names = {n for ns in tier_tools_from_config().values() for n in ns}
    assert local_tool_names() == config_names


# ---------------------------------------------------------------------------
# Config invariants — the sync points that must never drift
# ---------------------------------------------------------------------------
def test_admission_group_is_a_real_group():
    assert CFG["agent_admission_group"] in CFG["groups"]


def test_session_memory_flag_is_declared_boolean():
    # deploy-agent bakes this into the container env; a missing or non-bool
    # value would silently disable memory (env comparison is string-exact)
    assert isinstance(CFG["agent"].get("session_memory"), bool)


def test_tier_scopes_are_registered_in_resource_server():
    rs = CFG["resource_server"]
    legal = {f"{rs['identifier']}/{s['name']}" for s in rs["scopes"]}
    granted = {s for spec in CFG["groups"].values()
               for s in spec.get("scopes", [])}
    required = {t["required_scope"] for t in CFG["gateways"]["tiers"]}
    assert granted <= legal, f"granted-but-unregistered: {granted - legal}"
    assert required <= legal, f"required-but-unregistered: {required - legal}"


def test_requires_reference_real_groups():
    for spec in CFG["groups"].values():
        for req in spec.get("requires", []):
            assert req in CFG["groups"]


# ---------------------------------------------------------------------------
# OpenAPI (external-API) targets — the spec IS the tool, so config-consistency
# checks are the only local tests possible (the HTTP call happens in the
# gateway; there is no body of ours to unit-test).
# ---------------------------------------------------------------------------
def openapi_targets():
    return [(tier, t) for tier in CFG["gateways"]["tiers"]
            for t in tier.get("openapi_targets", [])]


def load_spec(t):
    return yaml.safe_load((HERE / t["spec_file"]).read_text())


def spec_operation_ids(spec):
    return [op["operationId"] for path in spec.get("paths", {}).values()
            for op in path.values()
            if isinstance(op, dict) and "operationId" in op]


def test_openapi_specs_exist_parse_and_pin_the_host():
    for tier, t in openapi_targets():
        spec = load_spec(t)
        assert spec.get("servers"), \
            f"{t['spec_file']}: no servers[] — the host must be pinned"
        assert all(s["url"].startswith("https://") for s in spec["servers"])
        assert spec_operation_ids(spec), f"{t['spec_file']}: no operationIds"


def test_openapi_tool_names_unique_and_disjoint_from_lambda_tools():
    lambda_names = {n for ns in tier_tools_from_config().values() for n in ns}
    for tier, t in openapi_targets():
        ids = spec_operation_ids(load_spec(t))
        assert len(ids) == len(set(ids)), f"{t['spec_file']}: duplicate operationIds"
        assert not (set(ids) & lambda_names), \
            f"{t['spec_file']}: operationId collides with a lambda tool name"


def test_openapi_credential_provider_config_complete():
    for tier, t in openapi_targets():
        cp = t.get("credential_provider") or {}
        for field in ("name", "env_var", "location", "parameter_name"):
            assert cp.get(field), \
                f"openapi target '{t.get('name')}': credential_provider.{field} missing"


def test_hidden_access_token_params_are_required():
    """The hide-and-inject convention: any gateway tool declaring an
    access_token parameter must mark it required — the Lambda depends on it
    for identity resolution, and the agent strips it from the model's view."""
    for tier in CFG["gateways"]["tiers"]:
        for t in tier.get("tools", []):
            schema = t["input_schema"]
            if "access_token" in schema.get("properties", {}):
                assert "access_token" in schema.get("required", []), \
                    f"tool '{t['name']}': access_token must be required"


def test_tools_lambda_include_paths_resolvable():
    """Every declarative bundle entry must be an existing file, or the
    S9-generated transactions.csv (absent until `create` runs)."""
    for inc in CFG["gateways"]["tools_lambda"].get("include", []):
        p = (HERE / inc).resolve()
        assert p.exists() or p.name == "transactions.csv", f"missing: {inc}"


def test_transactions_template_shape():
    import csv
    rows = list(csv.DictReader(
        (HERE / "data" / "transactions_template.csv").open()))
    per_user = {}
    for r in rows:
        per_user.setdefault(r["user_id"], []).append(int(r["days_ago"]))
    assert len(per_user) >= len(CFG.get("users", [])) or len(per_user) >= 2
    for user, days in per_user.items():
        assert len(days) == 5, f"dataset {user}: expected 5 rows"
        assert all(0 <= d <= 60 for d in days), f"dataset {user}: days_ago range"


def test_convert_currency_is_premium_only():
    by_scope = {}
    for tier in CFG["gateways"]["tiers"]:
        ids = []
        for t in tier.get("openapi_targets", []):
            ids += spec_operation_ids(load_spec(t))
        by_scope[tier["required_scope"]] = ids
    assert "convert_currency" in by_scope.get(PREMIUM, []), \
        "convert_currency must be attached to the premium tier"
    assert "convert_currency" not in by_scope.get(BASIC, []), \
        "convert_currency must NOT be attached to the basic tier"


# ---------------------------------------------------------------------------
# Session-identity binding (langgraph_bedrock entrypoint, Layer 2). The agent
# rejects any Runtime session id that isn't the caller's own finance-<sub>,
# so a direct caller can't target another subject's session. Same predicate
# as the entrypoint; a None session (no header / local invoke) is allowed
# because there is nothing to cross-target.
# ---------------------------------------------------------------------------
def session_allowed(caller_session, sub):
    expected = f"finance-{sub}"
    return caller_session is None or caller_session == expected


def test_session_matches_own_sub_allowed():
    assert session_allowed("finance-abc-123", "abc-123")


def test_session_forged_for_other_user_rejected():
    # Mallory (sub 'mallory') presenting Sarah's session is refused, even
    # though her own token is valid.
    assert not session_allowed("finance-sarah", "mallory")


def test_session_absent_allowed():
    # direct/local invocation without the header: nothing to cross-target
    assert session_allowed(None, "abc-123")


def test_arbitrary_session_rejected():
    assert not session_allowed("not-even-finance-prefixed", "abc-123")
