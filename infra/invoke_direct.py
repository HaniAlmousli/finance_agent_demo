"""
invoke_direct.py — call the AgentCore Runtime directly, bypassing finance_app.

Isolates failures: if this works but the web app fails, the problem is
app-side (finance_app.py / the browser); if this fails too, the problem is
the agent — read its CloudWatch logs:

    aws logs tail "/aws/bedrock-agentcore/runtimes/<agent_id>-DEFAULT" \
        --since 10m --profile <admin_profile> --region us-east-1 --format short

Usage (from this directory, server NOT required):

    uv run python invoke_direct.py sarah@example.com "what is the weather?"
    uv run python invoke_direct.py sarah@example.com --stop

--stop force-terminates the user's runtime session. Sessions are STICKY: each
user's session runs in a microVM created with the env of the deployment that
was live at first contact, and it survives redeploys while the user keeps
chatting. After a `deploy-agent`, stop the session (or stay idle ~15 min) so
the next message starts a fresh microVM with the new env.

Auth notes (mirrors finance_app.py's /chat):
  - The Runtime has a Cognito customJWTAuthorizer, so calls carry the USER'S
    bearer token — SigV4/IAM plays no part (the aws CLI equivalents fail with
    "Authorization method mismatch").
  - The session id MUST be finance-<sub>: the agent's entrypoint rejects any
    other value (session-identity binding), and the Runtime requires >=33 chars.
  - Passwords come from users.local.yaml; the client id/secret from Secrets
    Manager via the app_profile — the same sources the app uses.
"""

import base64
import hashlib
import hmac
import json
import sys
import urllib.parse
from pathlib import Path

import boto3
import requests
import yaml

HERE = Path(__file__).parent


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__.split("Usage")[1])
    username, action = sys.argv[1], sys.argv[2]

    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    out = json.loads((HERE / "infra_outputs.json").read_text())
    users = yaml.safe_load((HERE / "users.local.yaml").read_text())["users"]
    try:
        password = next(u["password"] for u in users if u["username"] == username)
    except StopIteration:
        sys.exit(f"{username} not in users.local.yaml "
                 f"(known: {[u['username'] for u in users]})")

    # Cognito app-client credentials — same vault read the app does at startup.
    session = boto3.Session(profile_name=cfg["app_profile"])
    secret = json.loads(
        session.client("secretsmanager", region_name=cfg["region"])
        .get_secret_value(SecretId=cfg["secret"]["name"])["SecretString"])
    client_id, client_secret = secret["client_id"], secret["client_secret"]

    # Login exactly like /login: USER_PASSWORD_AUTH + SECRET_HASH.
    cognito = boto3.client("cognito-idp", region_name=cfg["region"])
    secret_hash = base64.b64encode(hmac.new(
        client_secret.encode(), (username + client_id).encode(),
        hashlib.sha256).digest()).decode()
    auth = cognito.initiate_auth(
        ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password,
                        "SECRET_HASH": secret_hash})
    token = auth["AuthenticationResult"]["AccessToken"]

    # sub + scopes from the token payload (display only — the agent re-verifies
    # the signature itself; this script never needs to).
    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    session_id = f"finance-{payload['sub']}"
    print(f"user    : {username}")
    print(f"groups  : {payload.get('cognito:groups', [])}")
    print(f"scopes  : {payload.get('scope', '')}")
    print(f"session : {session_id}")

    arn = urllib.parse.quote(out["agent_arn"], safe="")
    base = f"https://bedrock-agentcore.{cfg['region']}.amazonaws.com/runtimes/{arn}"
    headers = {"Authorization": f"Bearer {token}",
               "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
               "Content-Type": "application/json"}

    if action == "--stop":
        r = requests.post(f"{base}/stopruntimesession?qualifier=DEFAULT",
                          headers=headers, json={}, timeout=30)
        print(f"\nstop -> HTTP {r.status_code}: {r.text[:300] or 'OK'}")
        return

    r = requests.post(f"{base}/invocations?qualifier=DEFAULT", headers=headers,
                      json={"prompt": action, "access_token": token}, timeout=120)
    print(f"\ninvoke -> HTTP {r.status_code}")
    try:
        print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(r.text[:1000])


if __name__ == "__main__":
    main()
