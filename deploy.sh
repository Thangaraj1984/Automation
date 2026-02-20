#!/bin/bash
# ============================================================
#  Deploy NSE Options Server to a user-configured VM
# ============================================================
#  Usage (from local Windows machine):
#    bash deploy.sh
#  Or from Git Bash / WSL:
#    ./deploy.sh
# ============================================================

set -e

CONFIG_FILE="./project.config"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Missing $CONFIG_FILE"
    echo "Copy project.config.example to project.config and update values first."
    exit 1
fi

set -a
source "$CONFIG_FILE"
set +a

VM_SSH_PORT="${VM_SSH_PORT:-22}"
NGINX_CONTAINER_NAME="${NGINX_CONTAINER_NAME:-nginx}"
DOCKER_NETWORK="${DOCKER_NETWORK:-app_network}"

required_vars=(VM_USER VM_HOST VM_DIR SSH_KEY_PATH APP_DOMAIN CERTBOT_EMAIL)
for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ] || [[ "${!var}" == *"YOUR_"* ]] || [[ "${!var}" == *"example.com"* ]]; then
        echo "❌ Invalid or missing value for $var in $CONFIG_FILE"
        exit 1
    fi
done

if [[ "$SSH_KEY_PATH" == ~/* ]]; then
    SSH_KEY_PATH="$HOME/${SSH_KEY_PATH#~/}"
fi

if [ ! -f "$SSH_KEY_PATH" ]; then
    echo "❌ SSH key not found: $SSH_KEY_PATH"
    exit 1
fi

TMP_NGINX_CONF="./nginx/nse.conf"
sed "s/__APP_DOMAIN__/$APP_DOMAIN/g" ./nginx/nse.conf.template > "$TMP_NGINX_CONF"

echo "============================================================"
echo "  Deploying NSE Options Server"
echo "============================================================"
echo ""

# ---- Step 1: Upload project files to VM ----
echo "[1/5] Uploading files to VM..."
ssh -p "$VM_SSH_PORT" -i "$SSH_KEY_PATH" "$VM_USER@$VM_HOST" "mkdir -p $VM_DIR/nginx; docker network create $DOCKER_NETWORK >/dev/null 2>&1 || true; docker network connect $DOCKER_NETWORK $NGINX_CONTAINER_NAME >/dev/null 2>&1 || true"

scp -P "$VM_SSH_PORT" -i "$SSH_KEY_PATH" \
    nse_server.py \
    NseUtility.py \
    requirements.txt \
    Dockerfile \
    docker-compose.yml \
    .dockerignore \
    project.config.example \
    "$VM_USER@$VM_HOST:$VM_DIR/"

scp -P "$VM_SSH_PORT" -i "$SSH_KEY_PATH" \
    "$TMP_NGINX_CONF" \
    "$VM_USER@$VM_HOST:$VM_DIR/nginx/"

echo "   ✅ Files uploaded"

# ---- Step 2: Copy nginx config into existing nginx container's config dir ----
echo "[2/5] Setting up nginx config..."
ssh -p "$VM_SSH_PORT" -i "$SSH_KEY_PATH" "$VM_USER@$VM_HOST" "NGINX_CONTAINER_NAME='$NGINX_CONTAINER_NAME' VM_DIR='$VM_DIR' bash -s" << 'REMOTE_NGINX'
    # Copy nse.conf to the nginx config directory
    # Find where nginx configs are mounted
    NGINX_CONF_DIR=$(docker inspect "$NGINX_CONTAINER_NAME" --format '{{ range .Mounts }}{{ if eq .Destination "/etc/nginx/conf.d" }}{{ .Source }}{{ end }}{{ end }}' 2>/dev/null || echo "")
    
    if [ -z "$NGINX_CONF_DIR" ]; then
        echo "   ⚠️  Could not auto-detect nginx conf.d mount."
        echo "   Trying common locations..."
        # Try common paths
        for dir in "$HOME/evolution-api/nginx/conf.d" \
               "$HOME/nginx/conf.d" \
                   /etc/nginx/conf.d; do
            if [ -d "$dir" ]; then
                NGINX_CONF_DIR="$dir"
                break
            fi
        done
    fi
    
    if [ -n "$NGINX_CONF_DIR" ]; then
        sudo cp "$VM_DIR/nginx/nse.conf" "$NGINX_CONF_DIR/nse.conf"
        echo "   ✅ Nginx config copied to $NGINX_CONF_DIR/nse.conf"
    else
        echo "   ❌ Could not find nginx conf.d directory!"
        echo "   Manually copy $VM_DIR/nginx/nse.conf to your nginx conf.d folder."
        exit 1
    fi
REMOTE_NGINX

# ---- Step 3: Obtain SSL certificate ----
echo "[3/5] Obtaining SSL certificate for $APP_DOMAIN..."
echo "   ⚠️  Make sure DNS A record for $APP_DOMAIN points to $VM_HOST first!"
ssh -p "$VM_SSH_PORT" -i "$SSH_KEY_PATH" "$VM_USER@$VM_HOST" << REMOTE_SSL
    # Check if cert already exists
    if docker exec certbot test -f /etc/letsencrypt/live/$APP_DOMAIN/fullchain.pem 2>/dev/null; then
        echo "   ✅ SSL cert for $APP_DOMAIN already exists"
    else
        echo "   Requesting new certificate..."
        docker exec certbot certbot certonly \
            --webroot \
            --webroot-path=/var/www/certbot \
            -d $APP_DOMAIN \
            --email $CERTBOT_EMAIL \
            --agree-tos \
            --no-eff-email \
            --non-interactive \
            || echo "   ⚠️  Certbot failed. Ensure DNS is pointing to this VM."
    fi
REMOTE_SSL

# ---- Step 4: Build and start the container ----
echo "[4/5] Building and starting container..."
ssh -p "$VM_SSH_PORT" -i "$SSH_KEY_PATH" "$VM_USER@$VM_HOST" << REMOTE_BUILD
    cd "$VM_DIR"
    
    # Build the image
    echo "   Building Docker image (this may take 2-3 minutes)..."
    DOCKER_NETWORK="$DOCKER_NETWORK" docker compose build --no-cache
    
    # Start the container
    echo "   Starting container..."
    DOCKER_NETWORK="$DOCKER_NETWORK" docker compose up -d
    
    echo "   ✅ Container started"
    docker compose ps
REMOTE_BUILD

# ---- Step 5: Reload nginx to pick up new config ----
echo "[5/5] Reloading nginx..."
ssh -p "$VM_SSH_PORT" -i "$SSH_KEY_PATH" "$VM_USER@$VM_HOST" << REMOTE_RELOAD
    docker exec "$NGINX_CONTAINER_NAME" nginx -t && docker exec "$NGINX_CONTAINER_NAME" nginx -s reload
    echo "   ✅ Nginx reloaded"
REMOTE_RELOAD

echo ""
echo "============================================================"
echo "  ✅ Deployment Complete!"
echo "============================================================"
echo ""
echo "  Public URL:  https://$APP_DOMAIN"
echo "  Health:      https://$APP_DOMAIN/api/health"
echo "  Sheets API:  https://$APP_DOMAIN/api/options/sheets"
echo ""
echo "  Update Google Sheets Apps Script:"
echo "    const SERVER_URL = \"https://$APP_DOMAIN\";"
echo ""
echo "  Monitor logs:"
echo "    ssh -p $VM_SSH_PORT -i $SSH_KEY_PATH $VM_USER@$VM_HOST"
echo "    docker logs -f nse-options-server"
echo ""
echo "============================================================"

rm -f "$TMP_NGINX_CONF"
