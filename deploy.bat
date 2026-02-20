@echo off
REM ============================================================
REM  Deploy NSE Options Server to a user-configured VM (Windows)
REM ============================================================
REM  Prerequisites:
REM    1) Copy project.config.example to project.config
REM    2) Update project.config with your own values
REM ============================================================

setlocal EnableDelayedExpansion

if not exist project.config (
	echo [ERROR] Missing project.config
	echo Copy project.config.example to project.config and fill your values.
	exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in (`findstr /r "^[A-Za-z_][A-Za-z0-9_]*=" project.config`) do (
	set "%%A=%%B"
)

if "%VM_SSH_PORT%"=="" set VM_SSH_PORT=22
if "%NGINX_CONTAINER_NAME%"=="" set NGINX_CONTAINER_NAME=nginx
if "%DOCKER_NETWORK%"=="" set DOCKER_NETWORK=app_network

if "%VM_USER%"=="" goto :missing
if "%VM_HOST%"=="" goto :missing
if "%VM_DIR%"=="" goto :missing
if "%SSH_KEY_PATH%"=="" goto :missing
if "%APP_DOMAIN%"=="" goto :missing
if "%CERTBOT_EMAIL%"=="" goto :missing
if not exist "%SSH_KEY_PATH%" (
	echo [ERROR] SSH key not found: %SSH_KEY_PATH%
	exit /b 1
)

powershell -NoProfile -Command "(Get-Content 'nginx/nse.conf.template') -replace '__APP_DOMAIN__','%APP_DOMAIN%' | Set-Content 'nginx/nse.conf'"

echo ============================================================
echo   Deploying NSE Options Server
echo ============================================================
echo.

REM ---- Step 1: Create remote directory ----
echo [1/5] Creating remote directory...
ssh -p "%VM_SSH_PORT%" -i "%SSH_KEY_PATH%" %VM_USER%@%VM_HOST% "mkdir -p %VM_DIR%/nginx; docker network create %DOCKER_NETWORK% > /dev/null 2>&1 || true; docker network connect %DOCKER_NETWORK% %NGINX_CONTAINER_NAME% > /dev/null 2>&1 || true"

REM ---- Step 2: Upload files ----
echo [2/5] Uploading project files...
scp -P "%VM_SSH_PORT%" -i "%SSH_KEY_PATH%" nse_server.py NseUtility.py requirements.txt Dockerfile docker-compose.yml .dockerignore project.config.example %VM_USER%@%VM_HOST%:%VM_DIR%/
scp -P "%VM_SSH_PORT%" -i "%SSH_KEY_PATH%" nginx\nse.conf %VM_USER%@%VM_HOST%:%VM_DIR%/nginx/
echo    Files uploaded.

REM ---- Step 3: Setup nginx config on VM ----
echo [3/5] Setting up nginx config on VM...
ssh -p "%VM_SSH_PORT%" -i "%SSH_KEY_PATH%" %VM_USER%@%VM_HOST% "NGINX_DIR=$(docker inspect %NGINX_CONTAINER_NAME% --format '{{ range .Mounts }}{{ if eq .Destination \"/etc/nginx/conf.d\" }}{{ .Source }}{{ end }}{{ end }}' 2>/dev/null); if [ -n \"$NGINX_DIR\" ]; then sudo cp %VM_DIR%/nginx/nse.conf $NGINX_DIR/nse.conf && echo 'Nginx config copied to '$NGINX_DIR; else echo 'ERROR: Could not find nginx conf.d mount'; fi"

echo [4/5] Obtaining SSL certificate...
ssh -p "%VM_SSH_PORT%" -i "%SSH_KEY_PATH%" %VM_USER%@%VM_HOST% "if docker exec certbot test -f /etc/letsencrypt/live/%APP_DOMAIN%/fullchain.pem 2>/dev/null; then echo 'SSL cert already exists'; else docker exec certbot certbot certonly --webroot --webroot-path=/var/www/certbot -d %APP_DOMAIN% --email %CERTBOT_EMAIL% --agree-tos --no-eff-email --non-interactive; fi"

REM ---- Step 5: Build and start container ----
echo [5/5] Building and starting container on VM...
ssh -p "%VM_SSH_PORT%" -i "%SSH_KEY_PATH%" %VM_USER%@%VM_HOST% "cd %VM_DIR% && DOCKER_NETWORK=%DOCKER_NETWORK% docker compose build --no-cache && DOCKER_NETWORK=%DOCKER_NETWORK% docker compose up -d && docker compose ps"

echo [DONE] Reloading nginx...
ssh -p "%VM_SSH_PORT%" -i "%SSH_KEY_PATH%" %VM_USER%@%VM_HOST% "docker exec %NGINX_CONTAINER_NAME% nginx -t && docker exec %NGINX_CONTAINER_NAME% nginx -s reload"

echo.
echo ============================================================
echo   Deployment Complete!
echo ============================================================
echo.
echo   Public URL:  https://%APP_DOMAIN%
echo   Health:      https://%APP_DOMAIN%/api/health
echo   Sheets API:  https://%APP_DOMAIN%/api/options/sheets
echo.
echo   Google Sheets Apps Script:
echo     const SERVER_URL = "https://%APP_DOMAIN%";
echo.
echo   Monitor logs:
echo     ssh -p %VM_SSH_PORT% -i %SSH_KEY_PATH% %VM_USER%@%VM_HOST%
echo     docker logs -f nse-options-server
echo.
echo ============================================================

del /q nginx\nse.conf >nul 2>&1

pause
exit /b 0

:missing
echo [ERROR] One or more required values are missing in project.config
exit /b 1
