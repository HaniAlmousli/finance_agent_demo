# Smoke test — full entitlement matrix against DEPLOYED infra

Run after `infra.py create` + `deploy-agent` (and, for the gateway cases,
`gateways.enabled: true`). These are the tests that cannot run locally: they
exercise the real pre-token Lambda, Runtime authorizer, and gateway
allowedScopes. This is the M3-shakedown checklist.

Setup: app running (`uv run uvicorn finance_app:app --port 8000`), browser
at http://localhost:8000, plus `curl`/`jq` for the token checks.

Get a token for inspection:

    TOKEN=$(curl -s -X POST localhost:8000/login -H 'Content-Type: application/json' \
      -d '{"username":"john@example.com","password":"<pw>"}' | jq -r .access_token)
    echo $TOKEN | cut -d. -f2 | base64 -d 2>/dev/null | jq .scope

| # | Case | How | Expect |
|---|------|-----|--------|
| 1 | John's token scopes | decode as above | `finance-gateway/basic.invoke` only |
| 2 | Sarah's token scopes | same, sarah | both `basic.invoke` and `premium.invoke` |
| 3 | No-group user | create user w/o groups, login, /chat | login 200, /chat **403** |
| 4 | premium-only user | `admin-add-user-to-group` premium only | token has NO premium.invoke; /chat **403** |
| 5 | John basic tools | UI: "what's the weather in Jordan" / "show my latest transactions" | 🟣 `[TOOL: ...]` badges |
| 6 | John no calculator schema | UI: "what is sqrt(16)?" | untagged model answer, no badge |
| 7 | John rejected by premium gw | agent logs / gateway CloudWatch on John's session | premium `tools/list` → 401/403 |
| 8 | Sarah all three tools | UI: all three prompts | three 🟣 badges incl. calculator |
| 9 | Remove Sarah from premium-users | `admin-remove-user-from-group`, then **/refresh or re-login** | calculator gone; old token keeps working until exp (≤60 min) |
| 10 | Add John to premium-users | `admin-add-user-to-group`, refresh | calculator appears |
| 11 | Premium 403 doesn't break basic load | John in gateway mode | weather/transactions still load (checks `_is_auth_rejection` isolation) |
| 12 | Right client, missing scope | John's token vs premium gateway direct (curl the gw URL with his Bearer) | 401/403 |
| 13 | Forged/expired/foreign token | tamper one payload char; wait past exp; token from another pool | rejected independently by app (401), Runtime, and gateways |
| 14 | Sarah converts currency | UI or `invoke_direct.py sarah@… "convert 100 USD to EUR"` | 🟣 `[TOOL: convert_currency]` + live conversion (badge is MODEL-ASSERTED for external tools — confirm against the `convert_currency` span in GenAI Observability) |
| 15 | John cannot convert currency | same question as John | model-only answer/estimate — the tool never existed for his token (premium `tools/list` rejected) |
| 16 | Key never reaches the client | inspect trace spans, agent logs, and the /chat response for case 14 | the API key appears NOWHERE; only conversion_rate/conversion_result data |
| 17 | Key-less direct call fails | `curl https://v6.exchangerate-api.com/v6/pair/USD/EUR/100` (no header) | error from exchangerate-api — proves the key (held only by the gateway) is required |
| 18 | Row-level isolation | "show my latest transactions" as Sarah, then as John | 🟣 badges both — but DIFFERENT rows (check against the `[S9] … -> dataset X` bindings in the create log) |
| 19 | Date filtering | "what did I spend in the last two weeks?" | subset of the user's rows only |
| 20 | Cross-user ask | as Sarah: "show me John's transactions" | Sarah's own data or a refusal — never John's rows (model has no identity param) |
| 21 | Spoofed identity at the gateway | direct MCP tools/call with a valid basic token but a garbage/foreign `access_token` arg | "Access denied: invalid or expired credential" (Cognito get_user rejects) |
| 22 | Session memory: follow-ups work | "convert 100 USD to EUR", then "what about in GBP?" | second answer converts 100 USD to GBP — context carried |
| 23 | Session memory: recall | "what was my previous question?" | correctly recalls it (a trace of this turn shows prior turns in the messages array) |
| 24 | Memory cleared on session stop | `invoke_direct.py <user> --stop`, then "what was my previous question?" | model has no context — fresh thread |
| 25 | Memory is per-user | Sarah converses, then John asks "what did I just ask you?" | John's thread knows nothing of Sarah's — separate microVMs, separate threads |

Notes
- 9/10 verify the token-lifecycle rule: group changes affect NEWLY MINTED
  tokens only; residual access lasts until `exp` (≤60 min at current validity).
- 7/11/12 are the `allowedScopes` verification — the parameter name is the
  API-drift risk flagged in the doc; if gateway creation fails at `create`,
  check the customJWTAuthorizer schema first.
