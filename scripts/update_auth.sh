#!/bin/bash
# ============================================================
# update_auth.sh — Update IIFL Auth on NSE Ingestor from anywhere
# ============================================================
# Works on: Termux (Android), macOS, Linux, Git Bash (Windows)
# Requirements: curl, ssh (both pre-installed on Termux)
#
# Install on Termux:
#   pkg install openssh curl
#   # Copy your SSH key to Termux:
#   mkdir -p ~/.ssh && chmod 700 ~/.ssh
#   # Transfer id_rsa to ~/.ssh/id_rsa (via file manager or scp)
#   chmod 600 ~/.ssh/id_rsa
#
# Usage:
#   bash update_auth.sh                     # interactive prompts
#   bash update_auth.sh <AUTH_CODE>          # pass auth code directly
#   bash update_auth.sh --jwt <JWT_TOKEN>    # pass pre-generated JWT directly
#
# Flow:
#   1. Takes your auth code
#   2. Exchanges it for a JWT (calls IIFL API with curl)
#   3. Sends the JWT to the VM container
#   4. Restarts the ingestor
# ============================================================

set -e

# === CONFIGURATION — loaded from .env (see .env.example) ===
# Resolve project root (one level up from scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_LOCAL="$SCRIPT_DIR/../.env"

# Auto-load .env if it exists
if [ -f "$ENV_LOCAL" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_LOCAL"
    set +a
fi

VM_IP="${VM_IP:-}"
VM_USER="${VM_USER:-azureuser}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
ENV_FILE="${VM_ENV_FILE:-/home/azureuser/nse-market-data/.env}"
CONTAINER="nse-data-ingestor"

# IIFL credentials — read from .env
CLIENT_ID="${IIFL_CLIENT_ID:-}"
API_SECRET="${IIFL_API_SECRET:-}"
API_URL="https://api.iiflcapital.com/v1/getusersession"

# =============================================

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

run_ssh() {
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$VM_USER@$VM_IP" "$1"
}

# --- Parse arguments ---
JWT_MODE=false
AUTH_CODE=""
JWT_TOKEN=""

if [ "$1" = "--jwt" ] && [ -n "$2" ]; then
    JWT_MODE=true
    JWT_TOKEN="$2"
elif [ -n "$1" ] && [ "$1" != "--help" ] && [ "$1" != "-h" ]; then
    AUTH_CODE="$1"
fi

# --- Help ---
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    echo "Usage:"
    echo "  bash update_auth.sh                    # interactive"
    echo "  bash update_auth.sh <AUTH_CODE>         # with auth code"
    echo "  bash update_auth.sh --jwt <JWT_TOKEN>   # with pre-generated JWT"
    echo ""
    echo "To get an auth code:"
    echo "  1. Open in browser:"
    echo "     https://markets.iiflcapital.com/?v=1&appkey=${IIFL_APP_KEY:-YOUR_APP_KEY}&redirecturl=http://localhost:3000/callback"
    echo "  2. Log in with IIFL credentials"
    echo "  3. After redirect, copy 'code' from URL: http://localhost:3000/callback?code=XXXXXXXXXX"
    echo "  4. Run: bash update_auth.sh XXXXXXXXXX"
    exit 0
fi

echo ""
echo "========================================"
echo "  NSE Ingestor — Auth Update Tool"
echo "========================================"
echo ""

# --- Get JWT (either from auth code or directly) ---
if [ "$JWT_MODE" = true ]; then
    info "Using provided JWT directly"
else
    # Get auth code if not passed
    if [ -z "$AUTH_CODE" ]; then
        echo -n "Enter IIFL Auth Code: "
        read -r AUTH_CODE
    fi

    if [ -z "$AUTH_CODE" ]; then
        fail "Auth code cannot be empty"
    fi

    info "Auth code: ${AUTH_CODE:0:6}..."

    # Compute SHA256 checksum: SHA256(clientId + authCode + apiSecret)
    info "Computing checksum..."
    RAW="${CLIENT_ID}${AUTH_CODE}${API_SECRET}"
    CHECKSUM=$(echo -n "$RAW" | sha256sum | cut -d' ' -f1)
    info "Checksum: ${CHECKSUM:0:16}..."

    # Exchange auth code for JWT
    info "Calling IIFL /getusersession..."
    RESPONSE=$(curl -s -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -H "AppName: BridgePy" \
        -H "AppVer: 1.0.0" \
        -H "OsName: Linux" \
        -d "{\"checkSum\": \"$CHECKSUM\"}")

    # Parse response
    STATUS=$(echo "$RESPONSE" | grep -o '"status":"[^"]*"' | head -1 | cut -d'"' -f4)
    JWT_TOKEN=$(echo "$RESPONSE" | grep -o '"userSession":"[^"]*"' | head -1 | cut -d'"' -f4)

    if [ -z "$JWT_TOKEN" ]; then
        echo ""
        fail "Failed to get JWT. Response: $RESPONSE"
    fi

    ok "Got JWT: ${JWT_TOKEN:0:20}...${JWT_TOKEN: -10}"
fi

JWT_LEN=${#JWT_TOKEN}
info "JWT length: $JWT_LEN chars"

if [ "$JWT_LEN" -lt 50 ]; then
    fail "JWT looks too short ($JWT_LEN chars). Something is wrong."
fi

# --- Update VM ---
echo ""
info "Connecting to VM ($VM_IP)..."

# Update both AUTH_CODE and SESSION_TOKEN in .env
info "Updating .env on VM..."
if [ -n "$AUTH_CODE" ]; then
    run_ssh "sed -i 's/^IIFL_AUTH_CODE=.*/IIFL_AUTH_CODE=$AUTH_CODE/' $ENV_FILE"
fi

# Add or update IIFL_SESSION_TOKEN
run_ssh "grep -q '^IIFL_SESSION_TOKEN=' $ENV_FILE && sed -i 's/^IIFL_SESSION_TOKEN=.*/IIFL_SESSION_TOKEN=$JWT_TOKEN/' $ENV_FILE || echo 'IIFL_SESSION_TOKEN=$JWT_TOKEN' >> $ENV_FILE"

ok "Updated .env"

# Restart ingestor
info "Restarting ingestor container..."
run_ssh "cd /home/azureuser/nse-market-data && docker compose up -d $CONTAINER"

# Wait for startup
info "Waiting 12 seconds for startup..."
sleep 12

# Check logs
info "Checking logs..."
echo ""
LOGS=$(run_ssh "docker logs $CONTAINER --tail 8 2>&1")
echo "$LOGS"
echo ""

# Check for success indicators
if echo "$LOGS" | grep -qi "pregenerated_jwt\|mqtt_connected\|auth_success"; then
    ok "Auth updated successfully! Ingestor is running with new JWT."
elif echo "$LOGS" | grep -qi "error\|failed"; then
    warn "Check the logs above — there may be an issue."
else
    info "Container is starting. Check status with:"
    echo "  ssh -i $SSH_KEY $VM_USER@$VM_IP 'docker logs $CONTAINER --tail 10 2>&1'"
fi

echo ""
echo "Done!"
