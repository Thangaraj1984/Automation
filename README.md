# NSE Options to Google Sheets (Public Template)

This project publishes NIFTY weekly options data from a Flask API to Google Sheets, including Greeks, buildups, and seller-panic signals.

## What was sanitized
- Added single local config file model (`project.config`) for deployment scripts.

## Files to keep in GitHub
- `nse_server.py`
- `NseUtility.py`
- `requirements.txt`
- `Dockerfile`
- `docker-compose.yml`
- `google_sheets_script.gs`
- `deploy.sh`
- `deploy.bat`
- `vm.ps1`
- `project.config.example`
- `nginx/nse.conf.template`
- `nginx/nse.conf` (placeholder sample)
- `.gitignore`
- `.dockerignore`
- `README.md`

## Files NOT to commit
- `project.config` (contains your private deployment values)
- `.venv/`
- SSH keys / cert files / local logs

## One-time setup
1. Copy config template:
   - `cp project.config.example project.config` (or copy manually on Windows)
2. Edit `project.config` with your values:
   - `VM_HOST`, `VM_USER`, `SSH_KEY_PATH`, `APP_DOMAIN`, `CERTBOT_EMAIL`, `DOCKER_NETWORK`, etc.
3. Ensure your DNS A record points `APP_DOMAIN` to `VM_HOST`.
4. Ensure an nginx + certbot setup exists on the VM.

## Deploy
- Linux/Git Bash/WSL: `bash deploy.sh`
- Windows CMD/PowerShell: `deploy.bat`

Both scripts read from `project.config`, render nginx config from `nginx/nse.conf.template`, create/connect the shared Docker network, upload app files, build container, and reload nginx.

## Google Sheets setup
1. Create/open a Google Sheet.
2. Open **Extensions → Apps Script**.
3. Paste `google_sheets_script.gs`.
4. Update only this value near the top:
   - `const SERVER_URL = "https://your-domain.example.com";`
5. Run `setupSheet()` once, then use menu actions.

## VM helper script
`vm.ps1` reads `project.config` and supports:
- `./vm.ps1 status`
- `./vm.ps1 start nse-options`
- `./vm.ps1 restart nse-options-server`
- `./vm.ps1 logs nse-options-server`

## Security checklist before publishing
- Confirm `project.config` is absent from git.
- Confirm no real domains/IPs/emails remain in tracked files.
- Confirm no private keys/certs are tracked.

## Notes
- Docker network in `docker-compose.yml` is `app_network` (`external: true`).
- Runtime network name can be overridden with `DOCKER_NETWORK` in `project.config`.
- Ensure your reverse-proxy container is attached to the same network.
- App API health endpoint: `/api/health`.
