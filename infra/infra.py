"""
infra.py — create / destroy all AWS infrastructure for finance_app
===================================================================

Provisions everything in Setup steps S1-S8 (and, optionally, the M3/M5
gateways) from the names in config.yaml. Nothing is
hardcoded here — edit config.yaml, not this file.

Usage:
    python infra.py create            # S1-S8 (idempotent: safe to re-run)
    python infra.py destroy           # tear everything down (asks first)
    python infra.py destroy --yes     # tear down without the prompt
    python infra.py status            # what currently exists

After `create`, it writes infra_outputs.json with the generated ids
(pool id, client id, ...) — finance_app.py reads its POOL_ID from there,
and `destroy` uses it to find what to delete.

Requires: pip install boto3 pyyaml
Runs as the ADMIN profile from config.yaml (not the app's runtime user).

Order matters and is dependency-driven (see the doc's Setup section):
  create : pool -> groups -> resource server -> lambda -> app client
           -> secret -> IAM user -> users -> [gateways]
  destroy: exact reverse.
"""

import argparse
import csv
import datetime
import getpass
import io
import json
import os
import random
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

HERE = Path(__file__).parent                  # infra/  — admin project
AGENT_DIR = HERE.parent / "agent"             # agent/  — the container build context
SERVER_DIR = HERE.parent / "server"           # server/ — the serving app
CONFIG_PATH = HERE / "config.yaml"
OUTPUTS_PATH = HERE / "infra_outputs.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
USERS_PATH = HERE / "users.local.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    # Users (with passwords) live in the gitignored users.local.yaml.
    # A `users:` block still present in config.yaml is honored as fallback.
    if USERS_PATH.exists():
        cfg["users"] = yaml.safe_load(USERS_PATH.read_text())["users"]
    elif "users" not in cfg:
        sys.exit("No users defined: copy users.local.yaml.example to "
                 "users.local.yaml and set passwords (see README).")
    return cfg


def load_outputs() -> dict:
    if OUTPUTS_PATH.exists():
        return json.loads(OUTPUTS_PATH.read_text())
    return {}


def save_outputs(outputs: dict) -> None:
    payload = json.dumps(outputs, indent=2)
    OUTPUTS_PATH.write_text(payload)
    print(f"   outputs -> {OUTPUTS_PATH.name}")
    # Mirror the handoff into server/ — the ONE file finance_app.py reads
    # (it never sees config.yaml). Deploying the server elsewhere = copy
    # server/ including this gitignored file.
    if SERVER_DIR.is_dir():
        (SERVER_DIR / "infra_outputs.json").write_text(payload)
        print(f"   outputs -> {SERVER_DIR.name}/infra_outputs.json (server handoff)")


def log(step: str, msg: str) -> None:
    print(f"[{step}] {msg}")


class Infra:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        session = boto3.Session(
            profile_name=cfg.get("admin_profile"), region_name=cfg["region"]
        )
        self.cognito = session.client("cognito-idp")
        self.secrets = session.client("secretsmanager")
        self.iam = session.client("iam")
        self.lam = session.client("lambda")
        self.sts = session.client("sts")
        self.agentcore_ctrl = session.client("bedrock-agentcore-control")
        self.ecr = session.client("ecr")
        self.account_id = self.sts.get_caller_identity()["Account"]
        self.out = load_outputs()

    # --- tagging helpers --------------------------------------------------
    # config.yaml `tags:` holds the shared keys; Service is per-resource.
    # Dict form  -> Cognito / Lambda / AgentCore TagResource & create_* Tags
    # List form  -> IAM / Secrets Manager / ECR ({Key, Value} pairs)
    def _tag_map(self, service: str) -> dict:
        tags = dict(self.cfg.get("tags") or {})
        tags["Service"] = service
        return tags

    def _tag_list(self, service: str) -> list:
        return [{"Key": k, "Value": v} for k, v in self._tag_map(service).items()]

    def _ensure_cognito_pool_tags(self, pool_id: str) -> None:
        arn = (f"arn:aws:cognito-idp:{self.cfg['region']}:"
               f"{self.account_id}:userpool/{pool_id}")
        self.cognito.tag_resource(ResourceArn=arn, Tags=self._tag_map("cognito"))

    def _ensure_lambda_tags(self, fn_arn: str) -> None:
        self.lam.tag_resource(Resource=fn_arn, Tags=self._tag_map("lambda"))

    def _ensure_iam_role_tags(self, role_name: str) -> None:
        self.iam.tag_role(RoleName=role_name, Tags=self._tag_list("iam"))

    def _ensure_iam_user_tags(self, user_name: str) -> None:
        self.iam.tag_user(UserName=user_name, Tags=self._tag_list("iam"))

    def _ensure_secret_tags(self, secret_name: str) -> None:
        arn = self.secrets.describe_secret(SecretId=secret_name)["ARN"]
        self.secrets.tag_resource(SecretId=arn, Tags=self._tag_list("secretsmanager"))

    def _ensure_agentcore_tags(self, resource_arn: str) -> None:
        self.agentcore_ctrl.tag_resource(
            resourceArn=resource_arn, tags=self._tag_map("bedrock-agentcore"))

    def _ensure_ecr_tags(self, repo_name: str) -> None:
        arn = (f"arn:aws:ecr:{self.cfg['region']}:"
               f"{self.account_id}:repository/{repo_name}")
        self.ecr.tag_resource(resourceArn=arn, tags=self._tag_list("ecr"))

    def _stamp_server_config(self) -> None:
        """Embed everything the SERVER needs into the outputs handoff, so
        finance_app.py reads ONE file (server/infra_outputs.json) and never
        needs config.yaml — the server project stays admin-config-free."""
        self.out["server_config"] = {
            "region": self.cfg["region"],
            "app_profile": self.cfg.get("app_profile"),
            "secret_name": self.cfg["secret"]["name"],
            "agent_admission_group": self.cfg["agent_admission_group"],
            "groups": list(self.cfg["groups"]),
        }

    # =======================================================================
    # CREATE — S1..S8 (+ optional gateways), each step idempotent
    # =======================================================================
    def create(self) -> None:
        self.s1_pool()
        self.s2_groups()
        self.s3_resource_server()
        self.s4_pre_token_lambda()
        self.s5_app_client()
        self.s6_secret()
        self.s7_iam_user()
        self.s8_users()
        self.s9_transactions_data()
        if self.cfg.get("gateways", {}).get("enabled"):
            self.gateways()
        elif self.out.get("gateways") or self.out.get("tools_lambda_arn"):
            # gateways.enabled flipped to false but they were built by a prior
            # run — RECONCILE: tear them down so config stays authoritative.
            # Otherwise they orphan in AWS (still costing) and their stale URLs
            # in infra_outputs.json would flip the agent to gateway mode.
            log("GW", "gateways.enabled=false — tearing down previously built "
                      "gateways to match config")
            self._destroy_gateways()
            self.out.pop("gateways", None)
            self.out.pop("tools_lambda_arn", None)
        self._stamp_server_config()
        save_outputs(self.out)
        if self.cfg.get("agent_runtime", {}).get("jwt_inbound_auth", True):
            print("\n✅ create complete. Next: `python infra.py deploy-agent` "
                  "(writes the agent ARN to infra_outputs.json), then start "
                  "the app — see README Part 2.")
        else:
            print("\n✅ create complete. Next: `python infra.py deploy-agent`, "
                  "put its ARN in config.yaml (iam.agent_runtime_arn), re-run "
                  "`create`, then start the app — see README Part 2.")

    # --- S1 ---------------------------------------------------------------
    def s1_pool(self) -> None:
        name = self.cfg["pool"]["name"]
        existing = self._find_pool(name)
        if existing:
            log("S1", f"pool '{name}' exists: {existing}")
            self.out["pool_id"] = existing
            self._ensure_cognito_pool_tags(existing)
            return
        resp = self.cognito.create_user_pool(
            PoolName=name,
            UsernameAttributes=["email"],
            AutoVerifiedAttributes=["email"],
            Policies={"PasswordPolicy": self.cfg["pool"]["password_policy"]},
            UserPoolTags=self._tag_map("cognito"),
        )
        self.out["pool_id"] = resp["UserPool"]["Id"]
        log("S1", f"created pool '{name}': {self.out['pool_id']}")

    def _find_pool(self, name: str):
        pages = self.cognito.get_paginator("list_user_pools").paginate(MaxResults=60)
        for page in pages:
            for p in page["UserPools"]:
                if p["Name"] == name:
                    return p["Id"]
        return None

    # --- S2 ---------------------------------------------------------------
    def s2_groups(self) -> None:
        for group in self.cfg["groups"]:
            try:
                self.cognito.create_group(
                    GroupName=group, UserPoolId=self.out["pool_id"]
                )
                log("S2", f"created group '{group}'")
            except self.cognito.exceptions.GroupExistsException:
                log("S2", f"group '{group}' exists")

    # --- S3 ---------------------------------------------------------------
    def s3_resource_server(self) -> None:
        rs = self.cfg["resource_server"]
        scopes = [
            {"ScopeName": s["name"], "ScopeDescription": s["description"]}
            for s in rs["scopes"]
        ]
        try:
            self.cognito.create_resource_server(
                UserPoolId=self.out["pool_id"],
                Identifier=rs["identifier"],
                Name=rs["name"],
                Scopes=scopes,
            )
            log("S3", f"created resource server '{rs['identifier']}' "
                      f"({len(scopes)} scopes)")
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidParameterException":
                raise
            # already exists -> update keeps scopes in sync with config
            self.cognito.update_resource_server(
                UserPoolId=self.out["pool_id"],
                Identifier=rs["identifier"],
                Name=rs["name"],
                Scopes=scopes,
            )
            log("S3", f"resource server '{rs['identifier']}' updated")

    # --- S4 ---------------------------------------------------------------
    def s4_pre_token_lambda(self) -> None:
        lcfg = self.cfg["pre_token_lambda"]
        fn_name, role_name = lcfg["function_name"], lcfg["role_name"]

        # 4a. execution role for the Lambda (logs only)
        try:
            role = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow",
                                   "Principal": {"Service": "lambda.amazonaws.com"},
                                   "Action": "sts:AssumeRole"}],
                }),
                Tags=self._tag_list("iam"),
            )
            self.iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            )
            log("S4", f"created lambda role '{role_name}' (waiting for IAM propagation)")
            time.sleep(10)  # new IAM roles take a moment to be assumable
            role_arn = role["Role"]["Arn"]
        except self.iam.exceptions.EntityAlreadyExistsException:
            role_arn = self.iam.get_role(RoleName=role_name)["Role"]["Arn"]
            self._ensure_iam_role_tags(role_name)
            log("S4", f"lambda role '{role_name}' exists")

        # 4b. the function code — GENERATED from config.yaml's groups mapping,
        # so config is the single source of truth for group -> scopes.
        # Schema per group: {scopes: [...], requires: [group, ...] (optional)}.
        # A group's scopes are granted ONLY if the user is also in every
        # `requires` group — e.g. premium-users alone must NOT yield
        # premium.invoke (premium is an add-on, not a substitute for base
        # membership).
        # Dedent BEFORE substituting the JSON: json.dumps output is flush-left,
        # so interpolating it first leaves the template lines with no common
        # leading whitespace and dedent strips nothing — the shipped module
        # then dies at import with "unexpected indent" and EVERY login 500s
        # (UserLambdaValidationException).
        code = textwrap.dedent("""\
            # Generated by infra.py from config.yaml — do not edit by hand.
            # Pre-token-generation trigger (V2_0): maps groups -> TIER scopes
            # at mint time. Conditional grants: a group's scopes apply only if
            # its `requires` groups are also present.
            GROUP_SCOPES = __GROUP_SCOPES__

            def handler(event, context):
                groups = set(event["request"]["groupConfiguration"]
                             .get("groupsToOverride") or [])
                scopes = set()
                for group, spec in GROUP_SCOPES.items():
                    if group not in groups:
                        continue
                    if not set(spec.get("requires", [])) <= groups:
                        continue
                    scopes.update(spec.get("scopes", []))
                event["response"]["claimsAndScopeOverrideDetails"] = {
                    "accessTokenGeneration": {"scopesToAdd": sorted(scopes)}
                }
                return event
        """).replace("__GROUP_SCOPES__", json.dumps(self.cfg["groups"], indent=4))
        compile(code, "index.py", "exec")   # malformed generation fails HERE, not at login
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("index.py", code)
        zipped = buf.getvalue()

        try:
            fn = self.lam.create_function(
                FunctionName=fn_name,
                Runtime=lcfg["runtime"],
                Role=role_arn,
                Handler="index.handler",
                Code={"ZipFile": zipped},
                Timeout=5,
                Tags=self._tag_map("lambda"),
            )
            fn_arn = fn["FunctionArn"]
            log("S4", f"created lambda '{fn_name}'")
        except self.lam.exceptions.ResourceConflictException:
            self.lam.update_function_code(FunctionName=fn_name, ZipFile=zipped)
            fn_arn = self.lam.get_function(FunctionName=fn_name)["Configuration"]["FunctionArn"]
            self._ensure_lambda_tags(fn_arn)
            log("S4", f"lambda '{fn_name}' code updated from config")
        self.out["lambda_arn"] = fn_arn

        # 4c. let Cognito invoke it
        try:
            self.lam.add_permission(
                FunctionName=fn_name,
                StatementId="cognito-pre-token",
                Action="lambda:InvokeFunction",
                Principal="cognito-idp.amazonaws.com",
                SourceArn=(f"arn:aws:cognito-idp:{self.cfg['region']}:"
                           f"{self.account_id}:userpool/{self.out['pool_id']}"),
            )
        except self.lam.exceptions.ResourceConflictException:
            pass  # permission already granted

        # 4d. wire the V2_0 trigger to the pool.
        # NOTE: V2_0 (access-token scope customization) requires the pool's
        # Essentials/Plus feature plan — create_user_pool defaults to it on
        # new AWS accounts; older accounts may need a console upgrade.
        self.cognito.update_user_pool(
            UserPoolId=self.out["pool_id"],
            AutoVerifiedAttributes=["email"],
            Policies={"PasswordPolicy": self.cfg["pool"]["password_policy"]},
            LambdaConfig={"PreTokenGenerationConfig": {
                "LambdaArn": fn_arn, "LambdaVersion": "V2_0"}},
        )
        log("S4", "wired PreTokenGeneration V2_0 trigger to the pool")

    # --- S5 ---------------------------------------------------------------
    # Cognito user-pool clients are not taggable (CreateUserPoolClient has
    # no Tags param); only the pool carries Cognito tags.
    def s5_app_client(self) -> None:
        acfg = self.cfg["app_client"]
        existing = self._find_client(acfg["name"])
        if existing:
            log("S5", f"app client '{acfg['name']}' exists: {existing}")
            self.out["client_id"] = existing
            return
        resp = self.cognito.create_user_pool_client(
            UserPoolId=self.out["pool_id"],
            ClientName=acfg["name"],
            GenerateSecret=True,
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH",
                               "ALLOW_REFRESH_TOKEN_AUTH"],
            AccessTokenValidity=acfg["access_token_validity_minutes"],
            IdTokenValidity=acfg["id_token_validity_minutes"],
            TokenValidityUnits={"AccessToken": "minutes", "IdToken": "minutes"},
        )
        client = resp["UserPoolClient"]
        self.out["client_id"] = client["ClientId"]
        # held only in memory, handed straight to S6
        self._client_secret = client["ClientSecret"]
        log("S5", f"created app client '{acfg['name']}': {client['ClientId']}")

    def _find_client(self, name: str):
        pages = self.cognito.get_paginator("list_user_pool_clients").paginate(
            UserPoolId=self.out["pool_id"], MaxResults=60)
        for page in pages:
            for c in page["UserPoolClients"]:
                if c["ClientName"] == name:
                    return c["ClientId"]
        return None

    # --- S6 ---------------------------------------------------------------
    def s6_secret(self) -> None:
        name = self.cfg["secret"]["name"]
        if not hasattr(self, "_client_secret"):
            # client pre-existed (S5 skipped) -> fetch its secret to (re)store
            desc = self.cognito.describe_user_pool_client(
                UserPoolId=self.out["pool_id"], ClientId=self.out["client_id"])
            self._client_secret = desc["UserPoolClient"]["ClientSecret"]
        payload = json.dumps({"client_id": self.out["client_id"],
                              "client_secret": self._client_secret})
        try:
            self.secrets.create_secret(
                Name=name, SecretString=payload,
                Tags=self._tag_list("secretsmanager"),
            )
            log("S6", f"created secret '{name}'")
        except self.secrets.exceptions.ResourceExistsException:
            self.secrets.put_secret_value(SecretId=name, SecretString=payload)
            self._ensure_secret_tags(name)
            log("S6", f"secret '{name}' updated")
        except ClientError as e:
            # scheduled-for-deletion from a prior destroy -> restore, then update
            if e.response["Error"]["Code"] != "InvalidRequestException":
                raise
            self.secrets.restore_secret(SecretId=name)
            self.secrets.put_secret_value(SecretId=name, SecretString=payload)
            self._ensure_secret_tags(name)
            log("S6", f"secret '{name}' restored from pending deletion + updated")

    # --- S7 ---------------------------------------------------------------
    def s7_iam_user(self) -> None:
        icfg = self.cfg["iam"]
        if not icfg.get("create_user", True):
            log("S7", f"skipped (iam.create_user=false) — app uses profile "
                      f"'{self.cfg.get('app_profile')}'")
            return

        user, policy = icfg["user_name"], icfg["policy_name"]

        statements = [{
            "Sid": "ReadOnlyFinanceAppClientSecret",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": (f"arn:aws:secretsmanager:{self.cfg['region']}:"
                         f"{self.account_id}:secret:{self.cfg['secret']['name']}-*"),
        }]
        # pre-M4: IAM inbound auth needs invoke rights on the runtime ARN
        arn = icfg.get("agent_runtime_arn") or ""
        if arn:
            statements.append({
                "Sid": "InvokeOnlyFinanceAgent",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                "Resource": [arn, f"{arn}/runtime-endpoint/*"],
            })
        else:
            log("S7", "NOTE: iam.agent_runtime_arn empty -> policy has no "
                      "InvokeAgentRuntime. Fill it in after deploying the "
                      "agent and re-run create (required pre-M4).")

        try:
            self.iam.create_user(UserName=user, Tags=self._tag_list("iam"))
            log("S7", f"created IAM user '{user}'")
        except self.iam.exceptions.EntityAlreadyExistsException:
            self._ensure_iam_user_tags(user)
            log("S7", f"IAM user '{user}' exists")

        self.iam.put_user_policy(     # put = create-or-replace, idempotent
            UserName=user, PolicyName=policy,
            PolicyDocument=json.dumps({"Version": "2012-10-17",
                                       "Statement": statements}),
        )
        log("S7", f"policy '{policy}' set ({len(statements)} statement(s))")
        # Inline policies are not taggable — only the IAM user carries tags.

        # access keys: create once; never print the secret to the console log
        app_profile = self.cfg.get("app_profile", "finance-app")
        keys = self.iam.list_access_keys(UserName=user)["AccessKeyMetadata"]
        if keys:
            log("S7", f"access key exists: {keys[0]['AccessKeyId']}")
        else:
            k = self.iam.create_access_key(UserName=user)["AccessKey"]
            print(textwrap.dedent(f"""
                [S7] NEW ACCESS KEY — add to ~/.aws/credentials NOW (shown once):

                    [{app_profile}]
                    aws_access_key_id = {k['AccessKeyId']}
                    aws_secret_access_key = {k['SecretAccessKey']}
            """))

    # --- S8 ---------------------------------------------------------------
    def s8_users(self) -> None:
        pool = self.out["pool_id"]
        for u in self.cfg["users"]:
            try:
                self.cognito.admin_create_user(
                    UserPoolId=pool, Username=u["username"],
                    UserAttributes=[
                        {"Name": "email", "Value": u["username"]},
                        {"Name": "email_verified", "Value": "true"},
                    ],
                    MessageAction="SUPPRESS",
                )
                log("S8", f"created user {u['username']}")
            except self.cognito.exceptions.UsernameExistsException:
                log("S8", f"user {u['username']} exists")
            self.cognito.admin_set_user_password(
                UserPoolId=pool, Username=u["username"],
                Password=u["password"], Permanent=True,
            )
            for g in u["groups"]:
                self.cognito.admin_add_user_to_group(
                    UserPoolId=pool, Username=u["username"], GroupName=g)
            log("S8", f"  groups: {u['groups']}")

    # --- S9 ---------------------------------------------------------------
    # Synthetic per-user transaction data for the get_transactions tool.
    # A fully SEPARATE, mode-independent step (to retire the feature: remove
    # this call from create(), the config include line, and the tool — no
    # packaging surgery). It binds each pool user to one synthetic dataset
    # (A–E) from data/transactions_template.csv, rewrites user_id -> the
    # user's REAL Cognito sub and days_ago -> a concrete date, and writes ONE
    # canonical artifact: agent/transactions.csv (gitignored). Consumers just
    # ship that file: the tools-Lambda zip via config's tools_lambda.include,
    # the agent container via deploy-agent's build context.
    def s9_transactions_data(self) -> None:
        template = HERE / "data" / "transactions_template.csv"
        rows = list(csv.DictReader(template.open()))
        letters = sorted({r["user_id"] for r in rows})
        users = self.cfg["users"]
        if len(users) > len(letters):
            sys.exit(f"S9: {len(users)} users but only {len(letters)} "
                     f"synthetic datasets in {template.name}")

        # Random binding (per request: random is fine; note that every
        # create re-run reshuffles which dataset each user gets).
        assigned = dict(zip((u["username"] for u in users),
                            random.sample(letters, len(users))))
        sub_for_letter = {}
        for u in users:
            resp = self.cognito.admin_get_user(
                UserPoolId=self.out["pool_id"], Username=u["username"])
            sub = next(a["Value"] for a in resp["UserAttributes"]
                       if a["Name"] == "sub")
            letter = assigned[u["username"]]
            sub_for_letter[letter] = sub
            log("S9", f"{u['username']} -> dataset {letter}")

        today = datetime.date.today()
        out_rows = [
            {"user_id": sub_for_letter[r["user_id"]],
             "date": (today - datetime.timedelta(days=int(r["days_ago"])))
                     .isoformat(),
             "description": r["description"], "amount": r["amount"],
             "currency": r["currency"], "category": r["category"]}
            for r in rows if r["user_id"] in sub_for_letter
        ]
        target = AGENT_DIR / "transactions.csv"
        with target.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["user_id", "date", "description",
                                              "amount", "currency", "category"])
            w.writeheader()
            w.writerows(out_rows)
        log("S9", f"wrote {len(out_rows)} transactions -> agent/{target.name}")

    # --- M3/M5 gateways (optional) ------------------------------------------
    # Build order (each step idempotent):
    #   1. tools Lambda (gateway_tools_lambda.py) + its execution role
    #   2. API-key credential providers (token-vault entries for external APIs)
    #   3. gateway exec role: lambda:InvokeFunction on the tools Lambda +
    #      GetResourceApiKey on the vault entries
    #   4. one gateway per tier (CUSTOM_JWT against the pool)
    #   5. attach each tier's Lambda tools + OpenAPI targets
    def gateways(self) -> None:
        gcfg = self.cfg["gateways"]
        tools_lambda_arn = self._gw_tools_lambda(gcfg["tools_lambda"])
        providers = self._gw_api_key_providers(gcfg["tiers"])
        role_arn = self._gw_exec_role(gcfg["exec_role_name"], tools_lambda_arn,
                                      providers)
        self._gw_create_gateways(gcfg["tiers"], role_arn)
        self._gw_attach_targets(gcfg["tiers"], tools_lambda_arn, providers)

    def _gw_api_key_providers(self, tiers: list) -> dict:
        """Ensure one AgentCore Identity API-KEY credential provider (a token-
        vault entry) per external API in config. Returns {name: {arn, secret_arn}}.

        The key is read from the provider's env_var — or, if unset AND the
        provider doesn't exist yet, prompted for interactively (hidden input;
        never echoed, never logged). Idempotent re-runs with the provider
        already in the vault need no key at all. Setting the env var again
        ROTATES the stored key (update_api_key_credential_provider)."""
        wanted = {}
        for tier in tiers:
            for t in tier.get("openapi_targets", []):
                cp = t["credential_provider"]
                wanted[cp["name"]] = cp
        if not wanted:
            return {}

        existing = {}
        token = None
        while True:
            kwargs = {"maxResults": 60}
            if token:
                kwargs["nextToken"] = token
            resp = self.agentcore_ctrl.list_api_key_credential_providers(**kwargs)
            for p in resp.get("credentialProviders", []):
                existing[p["name"]] = p["credentialProviderArn"]
            token = resp.get("nextToken")
            if not token:
                break

        # apiKeySecretArn in these responses is a STRUCTURE {"secretArn": ...},
        # not a string — extract it, or the raw dict ends up inside an IAM
        # policy Resource list and PutRolePolicy fails with MalformedPolicy.
        def secret_arn_of(field) -> str:
            return field.get("secretArn", "") if isinstance(field, dict) \
                else (field or "")

        providers = {}
        self.out.setdefault("credential_providers", {})
        for name, cp in wanted.items():
            key = os.environ.get(cp["env_var"], "")
            if name in existing and not key:
                # authoritative ARNs from AWS — never from possibly-stale
                # outputs (the secretsmanager statement must survive re-runs)
                info = self.agentcore_ctrl.get_api_key_credential_provider(
                    name=name)
                providers[name] = {
                    "arn": info.get("credentialProviderArn", existing[name]),
                    "secret_arn": secret_arn_of(info.get("apiKeySecretArn")),
                }
                log("GW", f"credential provider '{name}' exists (set "
                          f"{cp['env_var']} and re-run create to rotate the key)")
            elif name in existing:
                resp = self.agentcore_ctrl.update_api_key_credential_provider(
                    name=name, apiKey=key)
                providers[name] = {"arn": resp["credentialProviderArn"],
                                   "secret_arn": secret_arn_of(
                                       resp.get("apiKeySecretArn"))}
                log("GW", f"credential provider '{name}' key ROTATED")
            else:
                if not key:
                    print(f"[GW] credential provider '{name}' needs its API key "
                          f"(env var {cp['env_var']} is not set).")
                    key = getpass.getpass(f"    Paste the key for '{name}' "
                                          "(input hidden): ").strip()
                if not key:
                    sys.exit(f"No API key provided for '{name}' — set "
                             f"{cp['env_var']} or enter it at the prompt.")
                resp = self.agentcore_ctrl.create_api_key_credential_provider(
                    name=name, apiKey=key)
                providers[name] = {"arn": resp["credentialProviderArn"],
                                   "secret_arn": secret_arn_of(
                                       resp.get("apiKeySecretArn"))}
                log("GW", f"created credential provider '{name}' (key stored "
                          "in the AgentCore Identity token vault)")
            self.out["credential_providers"][name] = providers[name]
        return providers

    def _gw_tools_lambda(self, lcfg: dict) -> str:
        """Deploy the ONE Lambda that backs every gateway tool."""
        fn_name, role_name = lcfg["function_name"], lcfg["role_name"]
        try:
            role = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow",
                                   "Principal": {"Service": "lambda.amazonaws.com"},
                                   "Action": "sts:AssumeRole"}],
                }),
                Tags=self._tag_list("iam"),
            )
            self.iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            )
            log("GW", f"created tools-lambda role '{role_name}' (waiting for IAM)")
            time.sleep(10)
            role_arn = role["Role"]["Arn"]
        except self.iam.exceptions.EntityAlreadyExistsException:
            role_arn = self.iam.get_role(RoleName=role_name)["Role"]["Arn"]
            self._ensure_iam_role_tags(role_name)

        code = (HERE / lcfg["code_file"]).read_text()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("index.py", code)
            # Declarative bundle: config lists what ships alongside the
            # handler (shared tool bodies, generated data files). Nothing
            # here is feature-specific — add or remove files in config's
            # tools_lambda.include, never in this code.
            for inc in lcfg.get("include", []):
                p = (HERE / inc).resolve()
                if not p.exists():
                    sys.exit(f"tools_lambda.include: '{inc}' not found — did "
                             "its producing step run? (transactions.csv is "
                             "written by `create` step S9)")
                z.writestr(p.name, p.read_text())
        zipped = buf.getvalue()

        # POOL_ID lets the Lambda reject valid tokens minted by a DIFFERENT
        # pool in this region (get_transactions' cross-pool iss check).
        env = {"Variables": {"POOL_ID": self.out["pool_id"]}}
        try:
            fn = self.lam.create_function(
                FunctionName=fn_name,
                Runtime=lcfg["runtime"],
                Role=role_arn,
                Handler="index.handler",
                Code={"ZipFile": zipped},
                Timeout=10,
                Environment=env,
                Tags=self._tag_map("lambda"),
            )
            fn_arn = fn["FunctionArn"]
            log("GW", f"created tools lambda '{fn_name}'")
        except self.lam.exceptions.ResourceConflictException:
            self.lam.update_function_code(FunctionName=fn_name, ZipFile=zipped)
            # env updates go through a separate API; wait out the code update
            # first (concurrent updates raise ResourceConflictException).
            self.lam.get_waiter("function_updated_v2").wait(FunctionName=fn_name)
            self.lam.update_function_configuration(FunctionName=fn_name,
                                                   Environment=env)
            fn_arn = self.lam.get_function(
                FunctionName=fn_name)["Configuration"]["FunctionArn"]
            self._ensure_lambda_tags(fn_arn)
            log("GW", f"tools lambda '{fn_name}' code + env updated")
        self.out["tools_lambda_arn"] = fn_arn
        return fn_arn

    def _gw_exec_role(self, role_name: str, tools_lambda_arn: str,
                      providers: dict | None = None) -> str:
        """The role the GATEWAY assumes to reach its targets. Its inline
        policy allows invoking exactly the tools Lambda — this is why the
        agent itself never needs (or has) any Lambda permissions — plus,
        when OpenAPI targets exist, retrieving exactly OUR API keys from the
        AgentCore Identity token vault. Nothing else in the account carries
        the key-retrieval grant: a compromised app/agent/Lambda gets
        AccessDenied before the vault is even consulted."""
        try:
            role = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow",
                                   "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                                   "Action": "sts:AssumeRole"}],
                }),
                Tags=self._tag_list("iam"),
            )
            role_arn = role["Role"]["Arn"]
            log("GW", f"created gateway exec role '{role_name}' (waiting for IAM)")
            time.sleep(10)
        except self.iam.exceptions.EntityAlreadyExistsException:
            role_arn = self.iam.get_role(RoleName=role_name)["Role"]["Arn"]
            self._ensure_iam_role_tags(role_name)

        statements = [{"Effect": "Allow",
                       "Action": "lambda:InvokeFunction",
                       "Resource": tools_lambda_arn}]
        if providers:
            # Outbound auth for OpenAPI targets: the gateway (wearing this
            # role) exchanges its workload identity for the stored API key at
            # call time. Scoped to the vault/identity namespace + the exact
            # provider entries and their backing secrets.
            region, acct = self.cfg["region"], self.account_id
            resources = [
                f"arn:aws:bedrock-agentcore:{region}:{acct}:token-vault/default*",
                f"arn:aws:bedrock-agentcore:{region}:{acct}:workload-identity-directory/default*",
            ] + [p["arn"] for p in providers.values() if p.get("arn")]
            statements.append({
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:GetResourceApiKey",
                           "bedrock-agentcore:GetWorkloadAccessToken",
                           "bedrock-agentcore:GetWorkloadAccessTokenForJWT"],
                "Resource": resources,
            })
            secret_arns = [p["secret_arn"] for p in providers.values()
                           if p.get("secret_arn")]
            if secret_arns:
                # The vault stores each key in an AWS-managed Secrets Manager
                # secret (apiKeySecretArn); the retrieval path reads it.
                statements.append({"Effect": "Allow",
                                   "Action": "secretsmanager:GetSecretValue",
                                   "Resource": secret_arns})
        self.iam.put_role_policy(   # create-or-replace, idempotent
            RoleName=role_name,
            PolicyName="invoke-tools-lambda",
            PolicyDocument=json.dumps({"Version": "2012-10-17",
                                       "Statement": statements}),
        )
        return role_arn

    def _find_gateway(self, name: str):
        """Look a gateway up by name in AWS (list_gateways has no name filter)."""
        token = None
        while True:
            kwargs = {"maxResults": 60}
            if token:
                kwargs["nextToken"] = token
            resp = self.agentcore_ctrl.list_gateways(**kwargs)
            for g in resp.get("items", []):
                if g.get("name") == name:
                    return g
            token = resp.get("nextToken")
            if not token:
                return None

    def _gw_create_gateways(self, tiers: list, role_arn: str) -> None:
        discovery = (f"https://cognito-idp.{self.cfg['region']}.amazonaws.com/"
                     f"{self.out['pool_id']}/.well-known/openid-configuration")
        self.out.setdefault("gateways", {})
        for tier in tiers:
            name = tier["name"]
            # allowedScopes is the TIER GATE: allowedClients alone cannot
            # separate John from Sarah (same app client) — without the scope
            # condition both users would pass both gateways. Exactly ONE
            # required tier scope per gateway (allowedScopes is any-match,
            # never an AND).
            desired_auth = {"customJWTAuthorizer": {
                "discoveryUrl": discovery,
                "allowedClients": [self.out["client_id"]],
                "allowedScopes": [tier["required_scope"]],
            }}

            # Find the gateway: recorded in outputs, or existing in AWS
            # (outputs are only saved at the END of create(), so a crashed
            # run — or a destroyed-and-recreated pool — can leave gateways
            # alive but unrecorded).
            gw_id = self.out["gateways"].get(name, {}).get("id")
            if not gw_id:
                found = self._find_gateway(name)
                if found:
                    gw_id = found["gatewayId"]
                    log("GW", f"adopted existing gateway '{name}': {gw_id}")

            if gw_id:
                info = self.agentcore_ctrl.get_gateway(gatewayIdentifier=gw_id)
                gw_arn = info.get("gatewayArn") or info.get("arn") or ""
                self.out["gateways"][name] = {"id": gw_id,
                                              "url": info.get("gatewayUrl", ""),
                                              "arn": gw_arn}
                # RECONCILE the authorizer with the CURRENT pool/client. A
                # surviving gateway after a pool destroy/recreate otherwise
                # keeps validating tokens against the DEAD pool — every
                # caller gets 401/403, which the agent swallows as "tier not
                # yours", and users silently lose all tools.
                if info.get("authorizerConfiguration") != desired_auth:
                    self.agentcore_ctrl.update_gateway(
                        gatewayIdentifier=gw_id,
                        name=name,
                        roleArn=role_arn,
                        protocolType="MCP",
                        authorizerType="CUSTOM_JWT",
                        authorizerConfiguration=desired_auth,
                    )
                    log("GW", f"gateway '{name}' authorizer RECONCILED to "
                              "current pool/client")
                    self._gw_wait_ready(gw_id, name)
                else:
                    log("GW", f"gateway '{name}' exists: {gw_id}")
                if gw_arn:
                    self._ensure_agentcore_tags(gw_arn)
                continue

            gw = self.agentcore_ctrl.create_gateway(
                name=name,
                roleArn=role_arn,
                protocolType="MCP",
                authorizerType="CUSTOM_JWT",
                authorizerConfiguration=desired_auth,
                tags=self._tag_map("bedrock-agentcore"),
            )
            gw_arn = gw.get("gatewayArn") or gw.get("arn", "")
            self.out["gateways"][name] = {"id": gw["gatewayId"],
                                          "url": gw.get("gatewayUrl", ""),
                                          "arn": gw_arn}
            log("GW", f"created gateway '{name}': {gw['gatewayId']}")

    def _gw_wait_ready(self, gw_id: str, name: str, timeout: int = 120) -> None:
        """Wait out an UPDATING gateway before targets are touched."""
        waited = 0
        while waited < timeout:
            status = self.agentcore_ctrl.get_gateway(
                gatewayIdentifier=gw_id).get("status", "READY")
            if status in ("READY", "ACTIVE"):
                return
            if status.endswith("FAILED"):
                sys.exit(f"gateway '{name}' entered status {status}")
            time.sleep(5)
            waited += 5
        log("GW", f"gateway '{name}' still not READY after {timeout}s — continuing")

    def _put_target(self, gw_name: str, gw_id: str, existing: dict,
                    target_name: str, target_config: dict, cred_config: list,
                    tools_desc: list) -> None:
        """Create-or-update one gateway target. Updating in place is what
        makes config.yaml/spec edits propagate — a skipped existing target
        would silently keep serving the OLD schema to the agent forever.

        AgentCore VALIDATES the exec role's permissions (lambda invoke / key
        retrieval) when a target is created or updated. Right after this run
        (re)wrote the role's inline policy, IAM's eventual consistency can
        make that check fail spuriously — retry briefly before giving up."""
        for attempt in range(6):
            try:
                if target_name in existing:
                    self.agentcore_ctrl.update_gateway_target(
                        gatewayIdentifier=gw_id,
                        targetId=existing[target_name],
                        name=target_name,
                        targetConfiguration=target_config,
                        credentialProviderConfigurations=cred_config,
                    )
                    log("GW", f"target '{target_name}' updated on '{gw_name}' "
                              f"({tools_desc})")
                else:
                    self.agentcore_ctrl.create_gateway_target(
                        gatewayIdentifier=gw_id,
                        name=target_name,
                        targetConfiguration=target_config,
                        credentialProviderConfigurations=cred_config,
                    )
                    log("GW", f"attached target '{target_name}' to '{gw_name}' "
                              f"({tools_desc})")
                return
            except ClientError as e:
                err = e.response["Error"]
                if (err["Code"] == "ValidationException"
                        and "lacks permission" in err.get("Message", "")
                        and attempt < 5):
                    log("GW", f"target '{target_name}': exec-role permission "
                              "not visible yet (IAM propagation) — retrying in 10s")
                    time.sleep(10)
                    continue
                raise

    def _gw_attach_targets(self, tiers: list, tools_lambda_arn: str,
                           providers: dict | None = None) -> None:
        """Targets — the tool->tier assignment. THIS is the entitlement
        split: a tool attached to the premium gateway exists only for tokens
        whose scope that gateway admits. Two target kinds per tier:
          - the Lambda target (tiers[].tools -> gateway_tools_lambda.py)
          - OpenAPI targets (tiers[].openapi_targets -> the gateway itself
            calls the external API, injecting the vault-held key; no Lambda)"""
        providers = providers or {}
        for tier in tiers:
            name = tier["name"]
            gw_id = self.out["gateways"][name]["id"]
            existing = {
                t["name"]: t["targetId"]
                for t in self.agentcore_ctrl.list_gateway_targets(
                    gatewayIdentifier=gw_id).get("items", [])
            }

            # --- Lambda target: the tier's in-house tools -------------------
            tool_schemas = [
                {"name": t["name"],
                 "description": t["description"],
                 "inputSchema": t["input_schema"]}
                for t in tier.get("tools", [])
            ]
            if tool_schemas:
                self._put_target(
                    name, gw_id, existing, f"{name}-tools",
                    {"mcp": {"lambda": {
                        "lambdaArn": tools_lambda_arn,
                        "toolSchema": {"inlinePayload": tool_schemas},
                    }}},
                    [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
                    [t["name"] for t in tool_schemas],
                )
            else:
                log("GW", f"tier '{name}' has no lambda tools in config")

            # --- OpenAPI targets: external APIs, keyed via the vault --------
            for t in tier.get("openapi_targets", []):
                spec_text = (HERE / t["spec_file"]).read_text()
                spec = yaml.safe_load(spec_text)
                op_ids = [op["operationId"]
                          for path in spec.get("paths", {}).values()
                          for op in path.values() if "operationId" in op]
                cp = t["credential_provider"]
                self._put_target(
                    name, gw_id, existing, f"{name}-{t['name']}",
                    {"mcp": {"openApiSchema": {"inlinePayload": spec_text}}},
                    [{"credentialProviderType": "API_KEY",
                      "credentialProvider": {"apiKeyCredentialProvider": {
                          "providerArn": providers[cp["name"]]["arn"],
                          "credentialLocation": cp.get("location", "HEADER"),
                          "credentialParameterName": cp.get("parameter_name",
                                                            "Authorization"),
                          "credentialPrefix": cp.get("prefix", "Bearer"),
                      }}}],
                    op_ids,
                )

    # =======================================================================
    # DEPLOY-AGENT — build + push + create the AgentCore Runtime.
    # The toolkit builds the image remotely via CodeBuild (no local Docker):
    # it zips the BUILD CONTEXT, uploads it to S3, and CodeBuild runs the
    # docker build/push on an ARM64 fleet.
    # =======================================================================
    def deploy_agent(self) -> None:
        try:
            from bedrock_agentcore_starter_toolkit import Runtime
        except ImportError:
            sys.exit("bedrock-agentcore-starter-toolkit missing — run "
                     "`uv sync` in infra/ first")

        # The toolkit uses Path.cwd() as the container build context (the
        # whole directory is zipped and COPY'd into the image). Run it from
        # agent/ so the image contains ONLY the agent's code — never
        # users.local.yaml, certs, config.yaml, or server code.
        os.chdir(AGENT_DIR)

        acfg = self.cfg["agent_runtime"]
        kwargs = dict(
            entrypoint=acfg["entrypoint"],
            auto_create_execution_role=True,
            auto_create_ecr=True,
            requirements_file=acfg["requirements_file"],
            region=self.cfg["region"],
            agent_name=acfg["name"],
        )
        if acfg.get("jwt_inbound_auth", True):
            # Target/M4 design: the Runtime validates the USER'S Cognito JWT
            # itself — matches finance_app.py's bearer-token /chat call.
            kwargs["authorizer_configuration"] = {"customJWTAuthorizer": {
                "discoveryUrl": (
                    f"https://cognito-idp.{self.cfg['region']}.amazonaws.com/"
                    f"{self.out['pool_id']}/.well-known/openid-configuration"),
                "allowedClients": [self.out["client_id"]],
            }}
            log("AGENT", "deploying with JWT inbound auth (customJWTAuthorizer)")
        else:
            log("AGENT", "deploying with IAM inbound auth — set "
                         "iam.agent_runtime_arn afterwards and re-run create")

        # Deploy-time config for the container. The agent reads MODE (gateway
        # vs local), model, and pool identity from these — NOT from the /chat
        # payload — so a direct Runtime caller can't flip mode, swap the model,
        # or inject MCP URLs.
        #
        # MODE is decided by config.yaml's gateways.enabled — the source of
        # truth for INTENT — not by whatever happens to be in infra_outputs.json
        # (a record of what got BUILT). This prevents drift in both directions:
        #   - enabled=false but stale gateway URLs still in outputs -> ignored,
        #   - enabled=true but a tier's URL is missing -> hard error, never a
        #     silent downgrade to local mode.
        gw_cfg = self.cfg.get("gateways", {})
        if gw_cfg.get("enabled"):
            out_gw = self.out.get("gateways", {})
            gateway_urls, missing = {}, []
            for tier in gw_cfg.get("tiers", []):
                url = out_gw.get(tier["name"], {}).get("url")
                if url:
                    gateway_urls[tier["name"]] = url
                else:
                    missing.append(tier["name"])
            if missing:
                sys.exit(f"gateways.enabled=true but no gateway URL for tier(s) "
                         f"{missing} in infra_outputs.json — run "
                         f"`python infra.py create` first to build the gateways.")
        else:
            gateway_urls = {}   # config says local mode: ignore any stale outputs

        agent_env = {
            "REGION": self.cfg["region"],
            "POOL_ID": self.out["pool_id"],
            "CLIENT_ID": self.out["client_id"],
            "AGENT_ADMISSION_GROUP": self.cfg["agent_admission_group"],
            "GATEWAY_URLS": json.dumps(gateway_urls),
            "AGENT_LLM": json.dumps(self.cfg.get("agent", {})),
            "AGENT_SESSION_MEMORY": "true" if self.cfg.get("agent", {})
                                    .get("session_memory") else "false",
        }
        log("AGENT", f"env: mode={'gateway' if gateway_urls else 'local'}, "
                     f"model={self.cfg.get('agent', {}).get('model_id', '(default)')}, "
                     f"session_memory={agent_env['AGENT_SESSION_MEMORY']}")

        log("AGENT", f"building + launching '{acfg['name']}' from {AGENT_DIR.name}/ "
                     "(remote CodeBuild build; first run takes a few minutes)...")
        rt = Runtime()
        rt.configure(**kwargs)
        launch = rt.launch(env_vars=agent_env)

        # wait for the endpoint to be READY, like the notebook's status cell
        status = rt.status().endpoint["status"]
        while status not in ("READY", "CREATE_FAILED", "DELETE_FAILED",
                             "UPDATE_FAILED"):
            log("AGENT", f"status: {status} — waiting...")
            time.sleep(10)
            status = rt.status().endpoint["status"]
        if status != "READY":
            sys.exit(f"deploy failed: endpoint status {status}")

        self.out["agent_arn"] = launch.agent_arn
        self.out["agent_id"] = launch.agent_id
        self.out["agent_ecr_uri"] = getattr(launch, "ecr_uri", "")
        # Toolkit auto-creates runtime + ECR without our tag kwargs — stamp
        # the standard tags onto whatever it produced.
        try:
            self._ensure_agentcore_tags(launch.agent_arn)
            log("AGENT", "tagged agent runtime")
        except ClientError as e:
            log("AGENT", f"runtime tags: {e.response['Error']['Code']} (skipping)")
        ecr_uri = self.out.get("agent_ecr_uri", "")
        if ecr_uri and "/" in ecr_uri:
            repo = ecr_uri.split("/")[1].split(":")[0]
            try:
                self._ensure_ecr_tags(repo)
                log("AGENT", f"tagged ECR repo '{repo}'")
            except ClientError as e:
                log("AGENT", f"ECR tags: {e.response['Error']['Code']} (skipping)")
        try:
            info = self.agentcore_ctrl.get_agent_runtime(
                agentRuntimeId=launch.agent_id)
            role_arn = info.get("roleArn") or ""
            if role_arn:
                self._ensure_iam_role_tags(role_arn.rsplit("/", 1)[-1])
                log("AGENT", f"tagged exec role '{role_arn.rsplit('/', 1)[-1]}'")
        except ClientError as e:
            log("AGENT", f"exec role tags: {e.response['Error']['Code']} (skipping)")
        self._stamp_server_config()
        save_outputs(self.out)
        log("AGENT", f"READY — arn: {launch.agent_arn}")
        print("\n✅ agent deployed. Start the app from server/ — see README Part 2.")

    def _destroy_agent(self) -> None:
        agent_id = self.out.get("agent_id")
        if not agent_id:
            # Outputs lost? Resolve by NAME from config, like the pool and
            # gateways — a missing infra_outputs.json must never orphan the
            # runtime.
            wanted = self.cfg.get("agent_runtime", {}).get("name", "")
            token = None
            while wanted and not agent_id:
                kwargs = {"maxResults": 60}
                if token:
                    kwargs["nextToken"] = token
                resp = self.agentcore_ctrl.list_agent_runtimes(**kwargs)
                for rt in resp.get("agentRuntimes", []):
                    if rt.get("agentRuntimeName") == wanted:
                        agent_id = rt["agentRuntimeId"]
                        log("AGENT", f"found runtime by name: {agent_id}")
                        break
                token = resp.get("nextToken")
                if not token:
                    break
        if not agent_id:
            log("AGENT", "no deployed agent recorded in outputs (or found by name)")
            return
        try:
            self.agentcore_ctrl.delete_agent_runtime(agentRuntimeId=agent_id)
            log("AGENT", f"deleted agent runtime '{agent_id}'")
        except ClientError as e:
            log("AGENT", f"runtime: {e.response['Error']['Code']} (skipping)")
        ecr_uri = self.out.get("agent_ecr_uri", "")
        if ecr_uri and "/" in ecr_uri:
            try:
                session = boto3.Session(profile_name=self.cfg.get("admin_profile"),
                                        region_name=self.cfg["region"])
                session.client("ecr").delete_repository(
                    repositoryName=ecr_uri.split("/")[1], force=True)
                log("AGENT", f"deleted ECR repo '{ecr_uri.split('/')[1]}'")
            except ClientError as e:
                log("AGENT", f"ECR: {e.response['Error']['Code']} (skipping)")

    # =======================================================================
    # DESTROY — exact reverse order of create
    # =======================================================================
    def destroy(self, assume_yes: bool = False) -> None:
        print("This will DELETE the following (from config.yaml + infra_outputs.json):")
        print(f"  agent runtime   : {self.out.get('agent_id') or '(none deployed)'} + its ECR repo")
        print(f"  gateways        : {list(self.out.get('gateways', {}).keys()) or '(none)'}")
        print(f"  credential prov.: {self._provider_names() or '(none)'} — the stored API key(s) are erased")
        if self.cfg["iam"].get("create_user", True):
            print(f"  IAM user + keys : {self.cfg['iam']['user_name']}")
        else:
            print(f"  IAM user + keys : (skipped — iam.create_user=false)")
        print(f"  secret          : {self.cfg['secret']['name']}")
        print(f"  lambda + role   : {self.cfg['pre_token_lambda']['function_name']}")
        print(f"  user pool       : {self.cfg['pool']['name']} "
              f"({self.out.get('pool_id', '?')}) — including ALL users/groups/clients")
        if not assume_yes and input("Type 'delete' to proceed: ") != "delete":
            print("aborted."); return

        self._destroy_agent()
        self._destroy_gateways()
        self._destroy_iam_user()
        self._destroy_secret()
        self._destroy_lambda()
        self._destroy_pool()

        OUTPUTS_PATH.unlink(missing_ok=True)
        # ... and the server's mirrored handoff — a stale copy would let the
        # app boot against ids that no longer exist.
        (SERVER_DIR / "infra_outputs.json").unlink(missing_ok=True)
        # S9 artifact: its user_id column holds the pool's subs, which just
        # died with the pool — the next create rebinds from the template.
        (AGENT_DIR / "transactions.csv").unlink(missing_ok=True)
        print("\n🧹 destroy complete.")

    def _destroy_gateways(self) -> None:
        # Resolve by NAME from config, not just from outputs: a lost
        # infra_outputs.json must never orphan live gateways (that exact gap
        # once left gateways validating against a dead pool).
        ids = {name: info["id"]
               for name, info in self.out.get("gateways", {}).items()}
        for tier in self.cfg.get("gateways", {}).get("tiers", []):
            if tier["name"] not in ids:
                found = self._find_gateway(tier["name"])
                if found:
                    ids[tier["name"]] = found["gatewayId"]
        for name, gw_id in ids.items():
            try:
                # targets must go first — and their deletion is ASYNC: calling
                # delete_gateway while a target is still DELETING gets refused,
                # which used to orphan the gateway. Drain, then delete.
                ts = self.agentcore_ctrl.list_gateway_targets(
                    gatewayIdentifier=gw_id).get("items", [])
                for t in ts:
                    self.agentcore_ctrl.delete_gateway_target(
                        gatewayIdentifier=gw_id, targetId=t["targetId"])
                waited = 0
                while ts and waited < 120:
                    time.sleep(5)
                    waited += 5
                    ts = self.agentcore_ctrl.list_gateway_targets(
                        gatewayIdentifier=gw_id).get("items", [])
                self.agentcore_ctrl.delete_gateway(gatewayIdentifier=gw_id)
                log("GW", f"deleted gateway '{name}'")
            except ClientError as e:
                log("GW", f"gateway '{name}': {e.response['Error']['Code']} (skipping)")
        self._delete_role(self.cfg["gateways"]["exec_role_name"], "GW")
        # tools Lambda + its role
        tl = self.cfg["gateways"].get("tools_lambda", {})
        if tl:
            try:
                self.lam.delete_function(FunctionName=tl["function_name"])
                log("GW", f"deleted tools lambda '{tl['function_name']}'")
            except self.lam.exceptions.ResourceNotFoundException:
                pass
            self._delete_role(tl["role_name"], "GW")
        # API-key credential providers (token-vault entries for external APIs)
        for name in self._provider_names():
            try:
                self.agentcore_ctrl.delete_api_key_credential_provider(name=name)
                log("GW", f"deleted credential provider '{name}'")
            except ClientError as e:
                log("GW", f"credential provider '{name}': "
                          f"{e.response['Error']['Code']} (skipping)")

    def _provider_names(self) -> list:
        return sorted({t["credential_provider"]["name"]
                       for tier in self.cfg.get("gateways", {}).get("tiers", [])
                       for t in tier.get("openapi_targets", [])})

    def _destroy_iam_user(self) -> None:
        if not self.cfg["iam"].get("create_user", True):
            log("S7", "skipped destroy (iam.create_user=false)")
            return
        user = self.cfg["iam"]["user_name"]
        try:
            for k in self.iam.list_access_keys(UserName=user)["AccessKeyMetadata"]:
                self.iam.delete_access_key(UserName=user, AccessKeyId=k["AccessKeyId"])
            for p in self.iam.list_user_policies(UserName=user)["PolicyNames"]:
                self.iam.delete_user_policy(UserName=user, PolicyName=p)
            self.iam.delete_user(UserName=user)
            log("S7", f"deleted IAM user '{user}' (+ keys, + policy)")
        except self.iam.exceptions.NoSuchEntityException:
            log("S7", f"IAM user '{user}' not found")

    def _destroy_secret(self) -> None:
        name = self.cfg["secret"]["name"]
        try:
            # no recovery window: a re-create must be able to reuse the name
            self.secrets.delete_secret(SecretId=name,
                                       ForceDeleteWithoutRecovery=True)
            log("S6", f"deleted secret '{name}'")
        except self.secrets.exceptions.ResourceNotFoundException:
            log("S6", f"secret '{name}' not found")

    def _destroy_lambda(self) -> None:
        fn = self.cfg["pre_token_lambda"]["function_name"]
        try:
            self.lam.delete_function(FunctionName=fn)
            log("S4", f"deleted lambda '{fn}'")
        except self.lam.exceptions.ResourceNotFoundException:
            log("S4", f"lambda '{fn}' not found")
        self._delete_role(self.cfg["pre_token_lambda"]["role_name"], "S4")

    def _delete_role(self, role_name: str, step: str) -> None:
        try:
            for p in self.iam.list_attached_role_policies(
                    RoleName=role_name)["AttachedPolicies"]:
                self.iam.detach_role_policy(RoleName=role_name,
                                            PolicyArn=p["PolicyArn"])
            for p in self.iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
                self.iam.delete_role_policy(RoleName=role_name, PolicyName=p)
            self.iam.delete_role(RoleName=role_name)
            log(step, f"deleted role '{role_name}'")
        except self.iam.exceptions.NoSuchEntityException:
            pass

    def _destroy_pool(self) -> None:
        pool_id = self.out.get("pool_id") or self._find_pool(self.cfg["pool"]["name"])
        if not pool_id:
            log("S1", f"pool '{self.cfg['pool']['name']}' not found")
            return
        # deleting the pool cascades: users, groups, clients, resource server
        self.cognito.delete_user_pool(UserPoolId=pool_id)
        log("S1", f"deleted pool '{self.cfg['pool']['name']}' ({pool_id})")

    # =======================================================================
    # STATUS
    # =======================================================================
    def status(self) -> None:
        pool_id = self._find_pool(self.cfg["pool"]["name"])
        print(f"pool '{self.cfg['pool']['name']}'   : {pool_id or 'MISSING'}")
        if pool_id:
            self.out["pool_id"] = pool_id
            client = self._find_client(self.cfg["app_client"]["name"])
            print(f"app client '{self.cfg['app_client']['name']}' : {client or 'MISSING'}")
            groups = self.cognito.list_groups(UserPoolId=pool_id)["Groups"]
            print(f"groups                 : {[g['GroupName'] for g in groups]}")
            users = self.cognito.list_users(UserPoolId=pool_id)["Users"]
            print(f"users                  : {[u['Username'] for u in users]}")
        for label, fn in [
            ("secret", lambda: self.secrets.describe_secret(
                SecretId=self.cfg["secret"]["name"])["Name"]),
            ("lambda", lambda: self.lam.get_function(
                FunctionName=self.cfg["pre_token_lambda"]["function_name"]
            )["Configuration"]["FunctionName"]),
        ]:
            try:
                print(f"{label:22} : {fn()}")
            except ClientError:
                print(f"{label:22} : MISSING")
        if self.cfg["iam"].get("create_user", True):
            try:
                print(f"{'IAM user':22} : {self.iam.get_user(UserName=self.cfg['iam']['user_name'])['User']['UserName']}")
            except ClientError:
                print(f"{'IAM user':22} : MISSING")
        else:
            print(f"{'IAM user':22} : skipped (app_profile={self.cfg.get('app_profile')})")
        for name, info in load_outputs().get("gateways", {}).items():
            print(f"gateway '{name}'       : {info['id']}")
        if self._provider_names():
            have = set()
            token = None
            while True:
                kwargs = {"maxResults": 60}
                if token:
                    kwargs["nextToken"] = token
                resp = self.agentcore_ctrl.list_api_key_credential_providers(**kwargs)
                have |= {p["name"] for p in resp.get("credentialProviders", [])}
                token = resp.get("nextToken")
                if not token:
                    break
            for name in self._provider_names():
                print(f"{'credential provider':22} : "
                      f"{name if name in have else f'{name} MISSING'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("command",
                        choices=["create", "deploy-agent", "destroy", "status"])
    parser.add_argument("--yes", action="store_true",
                        help="destroy without the confirmation prompt")
    args = parser.parse_args()

    infra = Infra(load_config())
    if args.command == "create":
        infra.create()
    elif args.command == "deploy-agent":
        infra.deploy_agent()
    elif args.command == "destroy":
        infra.destroy(assume_yes=args.yes)
    else:
        infra.status()


if __name__ == "__main__":
    main()
