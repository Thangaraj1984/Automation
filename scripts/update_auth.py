#!/usr/bin/env python3
"""Update IIFL Auth (AuthCode + JWT) on VM and restart the ingestor.

Usage:
    python scripts/update_auth.py <AUTH_CODE>           # exchanges for JWT, sends both
    python scripts/update_auth.py --jwt <JWT_TOKEN>     # sends pre-generated JWT directly

Flow:
1. Takes auth code -> calls IIFL API -> gets JWT
2. Updates both IIFL_AUTH_CODE and IIFL_SESSION_TOKEN in .env on VM
3. Restarts the ingestor container (uses JWT directly, no re-exchange)
"""
import subprocess
import sys
import os
import hashlib
import json

try:
    import requests
except ImportError:
    print("Install requests: pip install requests")
    sys.exit(1)

def _load_env_file():
    """Load .env from the project root into os.environ (if present)."""
    env_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '.env'))
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())

_load_env_file()

VM_IP     = os.environ.get("VM_IP", "")
VM_USER   = os.environ.get("VM_USER", "azureuser")
SSH_KEY   = os.environ.get("SSH_KEY", os.path.expanduser("~/.ssh/id_rsa"))
ENV_FILE  = os.environ.get("VM_ENV_FILE", "/home/azureuser/nse-market-data/.env")
CONTAINER = "nse-data-ingestor"

CLIENT_ID  = os.environ.get("IIFL_CLIENT_ID", "")
API_SECRET = os.environ.get("IIFL_API_SECRET", "")
API_URL    = "https://api.iiflcapital.com/v1/getusersession"


def run_ssh(cmd: str) -> str:
    """Run a command on the VM via SSH."""
    result = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
         f"{VM_USER}@{VM_IP}", cmd],
        capture_output=True, text=True
    )
    if result.returncode != 0 and result.stderr:
        print(f"  SSH warning: {result.stderr.strip()}", file=sys.stderr)
    return result.stdout.strip()


def get_jwt_from_authcode(auth_code: str) -> str:
    """Exchange auth code for JWT via IIFL API."""
    raw = f"{CLIENT_ID}{auth_code}{API_SECRET}"
    checksum = hashlib.sha256(raw.encode()).hexdigest()
    print(f"  Checksum: {checksum[:16]}...")

    resp = requests.post(API_URL, json={"checkSum": checksum}, headers={
        "Content-Type": "application/json",
        "AppName": "BridgePy",
        "AppVer": "1.0.0",
        "OsName": "Linux",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    token = data.get("userSession") or data.get("data", {}).get("userSession")
    if not token:
        print(f"  Auth failed: {json.dumps(data, indent=2)}")
        sys.exit(3)
    return token


def main():
    auth_code = None
    jwt_token = None

    if len(sys.argv) >= 3 and sys.argv[1] == "--jwt":
        jwt_token = sys.argv[2].strip()
        print(f"Using provided JWT: {jwt_token[:20]}...{jwt_token[-10:]}")
    elif len(sys.argv) >= 2 and sys.argv[1] not in ("--help", "-h"):
        auth_code = sys.argv[1].strip()
    else:
        print("Usage:")
        print("  python update_auth.py <AUTH_CODE>         # exchange for JWT")
        print("  python update_auth.py --jwt <JWT_TOKEN>   # use pre-generated JWT")
        print("")
        print("To get an auth code:")
        print("  1. Open in browser:")
        app_key = os.environ.get("IIFL_APP_KEY", "YOUR_APP_KEY")
        print(f"     https://markets.iiflcapital.com/?v=1&appkey={app_key}&redirecturl=http://localhost:3000/callback")
        print("  2. Log in with IIFL credentials")
        print("  3. After redirect, copy 'code' from URL: http://localhost:3000/callback?code=XXXXXXXXXX")
        print("  4. Run: python update_auth.py XXXXXXXXXX")
        sys.exit(1)

    # Get JWT from auth code if needed
    if not jwt_token:
        print(f"1. Exchanging auth code ({auth_code[:6]}...) for JWT...")
        jwt_token = get_jwt_from_authcode(auth_code)
        print(f"   Got JWT: {jwt_token[:20]}...{jwt_token[-10:]}")

    print(f"\n2. Updating .env on VM ({VM_IP})...")
    if auth_code:
        run_ssh(f"sed -i 's/^IIFL_AUTH_CODE=.*/IIFL_AUTH_CODE={auth_code}/' {ENV_FILE}")
    run_ssh(f"grep -q '^IIFL_SESSION_TOKEN=' {ENV_FILE} && sed -i 's/^IIFL_SESSION_TOKEN=.*/IIFL_SESSION_TOKEN={jwt_token}/' {ENV_FILE} || echo 'IIFL_SESSION_TOKEN={jwt_token}' >> {ENV_FILE}")
    print("   .env updated with AUTH_CODE + JWT")

    print(f"\n3. Restarting {CONTAINER}...")
    run_ssh(f"cd /home/azureuser/nse-market-data && docker compose up -d {CONTAINER}")

    import time
    print("   Waiting 12s for startup...")
    time.sleep(12)

    print("\n4. Checking logs...")
    logs = run_ssh(f"docker logs {CONTAINER} --tail 8 2>&1")
    print(logs)

    if "mqtt_connected" in logs.lower() or "pregenerated_jwt" in logs.lower():
        print("\n✅ Auth updated! Ingestor running with new JWT.")
    else:
        print("\n⚠️  Check logs above for errors.")


if __name__ == "__main__":
    main()
