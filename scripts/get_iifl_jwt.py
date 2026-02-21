#!/usr/bin/env python3
"""Generate IIFL `userSession` (JWT) from ClientID + AuthCode + APISecret.

- Reads values from CLI flags or environment variables:
  IIFL_CLIENT_ID, IIFL_AUTH_CODE, IIFL_API_SECRET
- Computes SHA256(ClientID + AuthCode + APISecret)
- POSTs to https://api.iiflcapital.com/v1/getusersession
- Prints the returned `userSession` (JWT) and can append it to an env file

Usage examples:
  # non-interactive (envs set)
  python scripts/get_iifl_jwt.py --save

  # interactive prompts
  python scripts/get_iifl_jwt.py

  # pass on CLI
  python scripts/get_iifl_jwt.py --client-id X --auth-code Y --api-secret Z --save --env-file .env

Security:
  - Do NOT paste the JWT or secrets into public channels.
  - The script will not echo secrets when prompted.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import sys
from getpass import getpass

try:
    import requests
except Exception as e:
    raise SystemExit("'requests' package required. Install with: pip install requests")

GETUSERSESSION_URL = "https://api.iiflcapital.com/v1/getusersession"
DEFAULT_ENV_FILE = ".env"


def compute_checksum(client_id: str, auth_code: str, api_secret: str) -> str:
    raw = f"{client_id}{auth_code}{api_secret}"
    return hashlib.sha256(raw.encode()).hexdigest()


def call_getusersession(checksum: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "AppName": "BridgePy",
        "AppVer": "1.0.0",
        "OsName": platform.system(),
    }
    resp = requests.post(GETUSERSESSION_URL, json={"checkSum": checksum}, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def save_token_to_envfile(env_file: str, token: str) -> None:
    # Read existing file (if any) and replace IIFL_SESSION_TOKEN entry, otherwise append
    lines = []
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    found = False
    out = []
    for ln in lines:
        if ln.strip().startswith("IIFL_SESSION_TOKEN="):
            out.append(f"IIFL_SESSION_TOKEN={token}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"IIFL_SESSION_TOKEN={token}")
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def mask_token(token: str) -> str:
    if not token or len(token) < 20:
        return token
    return token[:8] + "..." + token[-8:]


def main():
    p = argparse.ArgumentParser(description="Exchange IIFL Auth Code for userSession (JWT)")
    p.add_argument("--client-id", help="IIFL Client ID (or set IIFL_CLIENT_ID env)")
    p.add_argument("--auth-code", help="Auth code from IIFL login (or set IIFL_AUTH_CODE env)")
    p.add_argument("--api-secret", help="IIFL API secret (or set IIFL_API_SECRET env)")
    p.add_argument("--save", action="store_true", help="Append IIFL_SESSION_TOKEN to .env (or --env-file)")
    p.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="Env file to write (default: .env)")
    args = p.parse_args()

    client_id = args.client_id or os.getenv("IIFL_CLIENT_ID")
    auth_code = args.auth_code or os.getenv("IIFL_AUTH_CODE")
    api_secret = args.api_secret or os.getenv("IIFL_API_SECRET")

    if not client_id:
        client_id = input("Client ID: ").strip()
    if not auth_code:
        auth_code = getpass("Auth Code (input hidden): ").strip()
    if not api_secret:
        api_secret = getpass("API Secret (input hidden): ").strip()

    missing = [name for name, val in (("Client ID", client_id), ("Auth Code", auth_code), ("API Secret", api_secret)) if not val]
    if missing:
        print("Missing:", ", ".join(missing))
        sys.exit(2)

    checksum = compute_checksum(client_id, auth_code, api_secret)
    print("Computed checksum:", checksum)

    try:
        data = call_getusersession(checksum)
    except requests.HTTPError as e:
        print("HTTP error while calling getusersession:", e)
        try:
            print(e.response.text)
        except Exception:
            pass
        sys.exit(3)
    except Exception as e:
        print("Error calling getusersession:", e)
        sys.exit(4)

    status = (data.get("status") or "").lower()
    token = data.get("userSession") or data.get("data", {}).get("userSession")
    if status == "ok" or token:
        print("\nSuccess — obtained userSession")
        print("Masked token:", mask_token(token))
        print("Full token (keep secret):\n", token)

        print("\nExport commands:")
        print(f"  Bash: export IIFL_SESSION_TOKEN='{token}'")
        print(f"  PowerShell: $env:IIFL_SESSION_TOKEN = '{token}'")

        if args.save:
            save_token_to_envfile(args.env_file, token)
            print(f"Saved IIFL_SESSION_TOKEN to {args.env_file}")

        sys.exit(0)
    else:
        print("Authentication failed; response:")
        print(json.dumps(data, indent=2))
        sys.exit(5)


if __name__ == "__main__":
    main()
