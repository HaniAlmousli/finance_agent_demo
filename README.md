# finance_app — Cognito-secured chat agent on Amazon Bedrock AgentCore

Demo project: a web app where users log in with username/password
(Cognito), then chat with a LangGraph agent hosted on Amazon Bedrock
AgentCore Runtime. Per-user **tool entitlement** is enforced cryptographically
via JWT scopes minted from Cognito groups — the agent's tools live behind
tiered AgentCore Gateways, and a user's token either admits them to a tier or
it doesn't.

**Demo entitlement model** (four tools, two tiers):


| User                                          | Groups                                  | Tools                                                                  |
| --------------------------------------------- | --------------------------------------- | ---------------------------------------------------------------------- |
| [sarah@example.com](mailto:sarah@example.com) | `finance-agent-users` + `premium-users` | weather, transactions (own rows), calculator, currency conversion      |
| [john@example.com](mailto:john@example.com)   | `finance-agent-users`                   | weather, transactions (**own rows only — no calculator, no currency**) |


What the demo shows end to end:

- **Login → scoped JWT:** Cognito group membership is turned into tier scopes
(`basic.invoke`, `premium.invoke`) by a pre-token-generation Lambda that is
*generated from config.yaml* — one source of truth.
- **Tiered tool access:** each AgentCore Gateway checks the caller's scope on
every call. John's token simply cannot reach the premium tools.
- **Row-level security:** both users hold `get_transactions`, but each only
ever sees their own rows — the user's verified access token is injected by
the agent runtime (hidden from the model) and re-verified by Cognito in the
tool Lambda.
- **External-API tool without exposing the key:** `convert_currency` is a pure
OpenAPI target — the gateway itself calls exchangerate-api.com, injecting an
API key stored in the AgentCore Identity token vault. The key never exists
in git, config, the container, the app, or the browser.
- **Session memory:** follow-up questions work per user, held in the user's
per-session microVM.

---



## Repository layout — three roots, two machine-roles


| Directory | What it is                                                                                                                                           | Which machine needs it                     |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `server/` | The serving app: FastAPI backend + web UI. Own `pyproject.toml` / `.venv`. Reads ONE runtime file: `server/infra_outputs.json` (the handoff).        | The serving machine — **and nothing else** |
| `infra/`  | The admin tier: `infra.py` CLI, `config.yaml` (single source of truth), demo user passwords, debug CLI, tests. Own `pyproject.toml` / `.venv`.       | Your admin workstation only                |
| `agent/`  | The **container build context** — exactly what ships to AgentCore Runtime: agent code, shared tool bodies, container deps. No venv (built remotely). | Neither — it ships to AWS                  |


Why `agent/` is its own root: the deploy pipeline zips **the whole build
directory** into the container image (`COPY . .`). Isolating the agent's
three files guarantees no secrets (passwords, certs, admin config) can ever
leak into ECR.

```
finance_app/
├── server/
│   ├── pyproject.toml          # fastapi, uvicorn, boto3, pyjwt, requests
│   ├── finance_app.py          # /login, /refresh, /chat + serves the UI
│   ├── static/index.html       # login form → chat (plain HTML+JS, no build step)
│   └── infra_outputs.json      # handoff written by infra.py (gitignored)
├── infra/
│   ├── pyproject.toml          # boto3, pyyaml, requests, starter toolkit
│   ├── infra.py                # create / deploy-agent / status / destroy
│   ├── config.yaml             # EVERY name & setting — edit this, not code
│   ├── gateway_tools_lambda.py # gateway-mode tool adapters (one Lambda)
│   ├── data/transactions_template.csv  # synthetic personas A–E; bound to real
│   │                           #   users by `create` step S9 → agent/transactions.csv
│   ├── specs/                  # OpenAPI specs for EXTERNAL-API tools (no code —
│   │   └── exchangerate-openapi.yaml   # the spec IS the convert_currency tool)
│   ├── users.local.yaml        # demo passwords (gitignored; copy the .example)
│   ├── invoke_direct.py        # debug CLI: call the Runtime directly / --stop sessions
│   └── tests/                  # local unit tests + smoke-test checklist
└── agent/
    ├── langgraph_bedrock.py    # the agent entrypoint (LangGraph)
    ├── tools_core.py           # SHARED tool bodies (also zipped into the gateway Lambda)
    └── requirements.txt        # the container's deps (installed by the remote build)
```

**Python environments (never mixed):** `server/.venv` and `infra/.venv` are
separate `uv` projects; the agent container's deps come from
`agent/requirements.txt` and are installed by the remote build; the tool
Lambdas are stdlib-only.

**Two AWS identities by design:** the **admin** profile (broad rights) runs
`infra.py` only; the **app** profile the server runs as needs exactly one
permission — reading one Cognito client secret. Users authenticate with their
own JWTs, so the server's IAM keys are never used to invoke the agent. In a
sandbox account without `iam:CreateUser`, set `iam.create_user: false` in
config.yaml and both profiles can be the same.

---



## Quickstart — fresh deploy + run, start to finish

**No Docker anywhere** — the container is built remotely by AWS CodeBuild.

Prerequisites:

- An AWS account and a credentials profile with admin rights. Set its name as
`admin_profile` (and `app_profile`) in `infra/config.yaml`.
- Bedrock model access for the model in `agent.model_id` (config.yaml).
- `[uv](https://docs.astral.sh/uv/)` installed.
- A free [exchangerate-api.com](https://www.exchangerate-api.com/) account
(**confirm the email** — unconfirmed accounts return inactive-account
errors); its API key feeds the premium `convert_currency` tool.

```bash
# 0. Sanity check — must return your ADMIN identity
aws sts get-caller-identity --profile <your-admin-profile>

# 1. Admin project setup
cd infra
uv sync
cp users.local.yaml.example users.local.yaml   # then edit: set real passwords
export EXCHANGE_RATE_API_KEY=<your key>        # first create only (else it prompts)

# 2. Provision identity/security infra (Cognito, Lambdas, secret, gateways,
#    external-API credential provider + OpenAPI target)
uv run python infra.py create                  # idempotent; writes the handoff
uv run python infra.py status                  # nothing should say MISSING

# 3. Build + deploy the agent (remote CodeBuild build from ../agent)
uv run python infra.py deploy-agent

# 4. Run the server (separate project, separate venv)
cd ../server
uv sync
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout key.pem -out cert.pem -subj "/CN=localhost"          # one-time cert
uv run uvicorn finance_app:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile key.pem --ssl-certfile cert.pem

# 5. Open https://localhost:8443 (accept the self-signed warning),
#    log in as sarah@ / john@example.com — see "Try it".
```

Order matters in steps 2–3: with `gateways.enabled: true` (the default
config), `deploy-agent` **hard-errors** unless `create` has already built the
gateways and recorded their URLs in the outputs.

### What `create` does (step 2)

Idempotent (safe to re-run; existing resources are adopted/updated, never
duplicated). In dependency order: Cognito pool → groups → scope vocabulary →
pre-token-generation Lambda (generated from config.yaml's `groups:` mapping —
this turns group membership into token scopes at login) → app client with
secret → secret in Secrets Manager → demo users → and, with gateways enabled,
the tiered AgentCore Gateways: the tools Lambda, the API-key credential
provider (your exchangerate key deposited into the AgentCore Identity token
vault), the gateway exec role, one gateway per tier, and each tier's Lambda
and OpenAPI tool targets.

It writes `infra_outputs.json` to `infra/` **and mirrors it to**
`server/` — the complete server handoff (ids + a `server_config` block).
The server reads only this file, never config.yaml.

### What `deploy-agent` does (step 3)

Points the AgentCore starter toolkit at `agent/` as the build context: zips
it to S3, has AWS CodeBuild build the ARM64 container remotely, pushes it to
an auto-created ECR repo, and launches the AgentCore Runtime with a **Cognito
JWT authorizer**. Polls until `READY` (a few minutes on first build) and
writes `agent_arn` into both copies of the handoff.

Two things are **baked into the container's environment** at deploy time
(deliberately not sent per request, so a direct Runtime caller can't override
them): the mode (gateway vs local, from `gateways.enabled`) and the LLM
(`agent.model_id` / `temperature`). Changing either means re-running
`deploy-agent`.

### When to re-run what

- change groups/scopes/users → edit `config.yaml` / `users.local.yaml`, re-run `create`
- change tool schemas or Lambda-side tool code → re-run `create` (in gateway
mode no redeploy needed — the agent loads schemas from the gateways per request)
- change agent code or `agent.model_id` → re-run `deploy-agent`
- flip `gateways.enabled` → `create` **then** `deploy-agent`

> **After every redeploy:** existing user sessions keep running on microVMs
> with the OLD container env while they stay active. Reset them:
> `uv run python invoke_direct.py sarah@example.com --stop` (and john@…) —
> a 404 just means the session was already gone.

---



## Running the app (server/, every run)

The serving machine needs exactly four things (no Docker, no starter toolkit,
no admin credentials, no `infra/` directory):

1. `cd server && uv sync`
2. The files: `server/` (code + `static/`) with `infra_outputs.json` in it
3. `~/.aws/credentials` with the profile named in the handoff's
  `server_config.app_profile`
4. Network egress to AWS (Cognito, Secrets Manager, the Runtime endpoint)

**Recommended (HTTPS, so you exercise the real login path):**

```bash
cd server
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout key.pem -out cert.pem -subj "/CN=localhost"   # one-time

uv run uvicorn finance_app:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile key.pem --ssl-certfile cert.pem
```

Open **[https://localhost:8443](https://localhost:8443)** and click through the self-signed-cert
warning (Advanced → Proceed).

**Plainest (HTTP, throwaway local runs only):**
`uv run uvicorn finance_app:app --host 0.0.0.0 --port 8000` → [http://localhost:8000](http://localhost:8000).
Works because the app's `require_https` guard exempts `localhost`. On any
non-localhost host the middleware rejects non-HTTPS requests with 403
(passwords transit `/login`); for a real deployment terminate TLS in a proxy
(ALB/nginx + `--proxy-headers`), or set `REQUIRE_HTTPS=false` to bypass the
guard for a plaintext test.

At startup the app reads the handoff, fetches the Cognito client secret from
Secrets Manager and the pool's public signing keys (JWKS), then fails fast
with a clear message if anything is missing.

## Try it — the demo script


| Ask                             | Sarah                                                | John                                                       |
| ------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------- |
| (header chips on login)         | `basic` + `premium` green                            | `premium` struck out                                       |
| "show my latest transactions"   | 🟣 `[TOOL: get_transactions @lambda]` + **her** rows | same badge — but **his** rows                              |
| "what's the weather in Jordan?" | 🟣 `[TOOL: get_weather @lambda]` … sunny             | same                                                       |
| "what is sqrt(16)?"             | 🟣 `[TOOL: calculator @lambda]` 4.0                  | untagged "4" — model knowledge, **no tool**                |
| "convert 100 USD to EUR"        | 🟣 `[TOOL: convert_currency]` + live conversion      | model-only estimate — the tool doesn't exist for his token |


Passwords are in `infra/users.local.yaml`. The purple `[TOOL: ...]` badge
proves the answer came from a tool; an untagged answer came from the model —
John asking for math is the entitlement story made visible. The `@lambda`
suffix proves the tool body ran in the gateway Lambda, not inside the agent
container. Weather rule: countries starting a–j are sunny, k–z cloudy
(deterministic).

Follow-ups work too (session memory): "convert that same amount to GBP",
"what was my previous question?". History lives in the user's per-session
microVM and dies with it (~15 min idle, the 8 h session cap, any
`deploy-agent`, or `invoke_direct.py <user> --stop`).

## Debugging without the app

`infra/invoke_direct.py` calls the AgentCore Runtime directly with a user's
JWT — no uvicorn needed:

```bash
cd infra
uv run python invoke_direct.py sarah@example.com "what is the weather in Jordan?"
uv run python invoke_direct.py sarah@example.com --stop     # kill a sticky session
```

Rule of thumb when something breaks: 502 in the app → `invoke_direct.py` to
bypass the app → if that fails too, read the agent's CloudWatch logs
(`/aws/bedrock-agentcore/runtimes/<agent_id>-DEFAULT`) or the CloudWatch
**GenAI Observability → Bedrock AgentCore** console (sessions, traces, and
per-invocation spans including tool inputs).

---



## Configuration quick reference (`infra/config.yaml`)


| Key                                | What it controls                                                                                                                                                                       |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `admin_profile`                    | AWS profile used by `infra.py` only — **set this first**                                                                                                                               |
| `app_profile`                      | AWS profile the server runs as (stamped into the handoff's `server_config`)                                                                                                            |
| `groups:`                          | group → TIER-scope mapping (with `requires:` conditionals — premium needs basic); **generates the pre-token Lambda**. Scopes are per-tier, never per-tool                              |
| `gateways.tiers[].tools`           | the tool→tier assignment AND each tool's model-visible schema (gateway mode); a unit test enforces name parity with local mode                                                         |
| `gateways.tiers[].required_scope`  | each gateway's `allowedScopes` gate                                                                                                                                                    |
| `gateways.tiers[].openapi_targets` | external-API tools: the OpenAPI spec file (the tool itself) + the credential provider (vault entry name, `env_var` for the one-time key deposit, header/prefix injection)              |
| `agent_admission_group`            | the ONE group required to use the agent at all                                                                                                                                         |
| `agent.model_id` / `temperature`   | the agent's LLM — baked into the container env at deploy time. Use a `us.*` inference profile if your org's SCP denies `global.*`                                                      |
| `agent.session_memory`             | per-session conversation memory. Baked in at deploy                                                                                                                                    |
| `agent_runtime.jwt_inbound_auth`   | `true` = Runtime validates user JWTs (default); `false` = legacy IAM invocation                                                                                                        |
| `iam.create_user`                  | `true` = create a dedicated least-privilege invoker user for the server; `false` for sandbox accounts that can't create IAM users                                                      |
| `users.local.yaml` (separate file) | demo users, passwords, group memberships — gitignored                                                                                                                                  |
| `gateways.enabled`                 | `true` (default) = tools run behind tiered AgentCore Gateways; `false` = tools run inside the agent container. Baked in at deploy — flipping requires `create` **then** `deploy-agent` |




## Tests

```bash
cd infra
uv run --with pytest --with langchain-core python -m pytest tests/ -q
```

`tests/test_entitlement.py` — local unit tests (no AWS): the pre-token
Lambda's conditional group→scope logic, the `/chat` admission gate, tool-name
parity, session-identity binding, and config-consistency invariants.
`tests/smoke_test.md` — the checklist to run against deployed infra.

## Teardown & cleanup

Stop the server (`Ctrl+C`), then:

```bash
cd infra
uv run python infra.py destroy     # prints exactly what it will delete; type `delete`
```

Deletes, in reverse dependency order: the agent runtime + its ECR repo → the
gateways + exec role → the tools Lambda + role → the API-key credential
provider (⚠️ your exchangerate key is erased from the vault — the next
`create` needs `EXCHANGE_RATE_API_KEY` again) → the secret → the pre-token
Lambda + role → the Cognito pool (cascades all users/groups/clients/scopes).
Resources are resolved **by name from config** if the outputs file is
missing, so a lost `infra_outputs.json` cannot orphan them.

What destroy does NOT remove (toolkit-owned leftovers, delete manually if you
want a zero-residue account): the CodeBuild project + role, the S3 sources
bucket (`bedrock-agentcore-*`), and CloudWatch log groups
(`/aws/bedrock-agentcore/runtimes/*`, `/aws/lambda/demo-*`,
`/aws/codebuild/*`).

## Troubleshooting


| Symptom                                                                      | Likely cause                                                                                                                                                                         |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| App won't start: handoff missing / no `server_config`                        | Run `python infra.py create` in `infra/` — it writes `server/infra_outputs.json`                                                                                                     |
| App won't start: "No agent deployed"                                         | Run `python infra.py deploy-agent` in `infra/`                                                                                                                                       |
| App won't start: can't read secret                                           | The profile named in the handoff's `server_config.app_profile` is missing/wrong in `~/.aws/credentials`                                                                              |
| Login works, all tool chips struck out / no tools ever fire                  | Pre-token Lambda not minting scopes — pool must be on Cognito's Essentials plan (V2_0 trigger); check `python infra.py status`                                                       |
| Login 500 ("Unexpected token …")                                             | The pre-token Lambda itself is broken — replay `initiate_auth` to see Cognito's error                                                                                                |
| Login 401 for a known-good password                                          | User not created / wrong pool — `python infra.py status`                                                                                                                             |
| Chat 403 "not entitled"                                                      | User not in `finance-agent-users`                                                                                                                                                    |
| Chat errors after ~60 min                                                    | Expected: token expiry → the page silently refreshes and retries once; persistent failure = the refresh token (30 d) expired — log in again                                          |
| `deploy-agent` fails at "Setting up AWS resources"                           | Wrong AWS account/identity reaching CodeBuild/ECR (the toolkit uses the default credential chain — check `AWS_PROFILE`), or the admin role lacks CodeBuild/ECR/S3 rights             |
| Chat 502; agent logs show `ResourceNotFoundException ... end of its life`    | `agent.model_id` retired by Bedrock — set a current inference-profile id and re-run `deploy-agent`                                                                                   |
| Chat 502; agent logs show `AccessDeniedException ... service control policy` | An org SCP blocks that model/profile — try `us.*` instead of `global.*`                                                                                                              |
| Chat still fails for one user right after a redeploy, works for others       | Sticky session on the old microVM — `uv run python invoke_direct.py <user> --stop`, or leave it idle ~15 min                                                                         |
| `convert_currency` answers report an API error                               | The model relays exchangerate-api's `error-type`: `invalid-key` (rotate/re-deposit the key), `inactive-account` (confirm the account email), `quota-reached` (free-tier monthly cap) |
| `convert_currency` fails; logs show `AccessDenied ... GetResourceApiKey`     | The gateway exec role's key-retrieval grant is missing (re-run `create`) — or an org SCP denies it                                                                                   |
| `get_transactions` says "Access denied: invalid or expired credential"       | The injected token failed Cognito's `get_user` (expired/revoked). The UI's silent refresh normally fixes expiry on retry                                                             |
| `get_transactions` says "Transaction data is not available"                  | `agent/transactions.csv` wasn't shipped — run `create` (S9 generates it) and, for local mode, `deploy-agent`                                                                         |




## Known limitations (not production-ready)

- **No rate limiting or concurrency controls** on `/login` and `/chat`: an
attacker could brute-force login or burn Bedrock/gateway/API quotas.
- **Entitlement revocation lag:** access tokens stay valid for up to 60
minutes after a user is removed from a group or disabled.
- `convert_currency` **provenance is model-asserted:** external APIs don't
emit the `[TOOL: ...]` tag, so the system prompt tells the model to add it
itself — weaker evidence than the in-house tools' data-carried tags. Ground
truth lives in the CloudWatch GenAI Observability traces.

