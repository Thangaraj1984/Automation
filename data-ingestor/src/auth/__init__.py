"""IIFL Capital Markets API Session Manager.

Handles authentication using the IIFL Capital Markets API:
- POST /getusersession with checksum = SHA256(clientId + authCode + apiSecret)
- Returns Bearer token for subsequent requests
"""
import hashlib
import httpx
import structlog
from ..config import Config

log = structlog.get_logger()


class SessionManager:
    """Manages IIFL Capital Markets API authentication session."""

    def __init__(self):
        self.session_token: str | None = None
        self.client_id = Config.IIFL_CLIENT_ID
        self._http_client = httpx.AsyncClient(
            base_url=Config.IIFL_BASE_URL,
            timeout=30.0,
        )

    def _compute_checksum(self, auth_code: str) -> str:
        """Compute SHA256 checksum: SHA256(clientId + authCode + apiSecret)"""
        raw = f"{Config.IIFL_CLIENT_ID}{auth_code}{Config.IIFL_API_SECRET}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def authenticate(self, auth_code: str | None = None, db_pool=None) -> str:
        """Authenticate and get session token.

        Priority order:
        1. JWT from database (set via /auth web page)
        2. IIFL_SESSION_TOKEN env var (pre-generated JWT)
        3. Exchange IIFL_AUTH_CODE for a new JWT

        Args:
            auth_code: Override auth code. If None, uses config value.
            db_pool: Optional asyncpg pool to check for JWT stored via web auth.

        Returns:
            Session token string.
        """
        # Priority 1: Check database for JWT set via /auth web page
        if db_pool:
            try:
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT value, updated_at FROM app_settings WHERE key = 'iifl_session_token'"
                    )
                    if row and row['value']:
                        self.session_token = row['value']
                        log.info("iifl_auth_using_db_jwt",
                                 client_id=Config.IIFL_CLIENT_ID,
                                 updated_at=str(row['updated_at']))
                        return self.session_token
            except Exception:
                pass  # Table may not exist yet, fall through

        # Priority 2: Pre-generated JWT from env var
        if Config.IIFL_SESSION_TOKEN:
            self.session_token = Config.IIFL_SESSION_TOKEN
            log.info("iifl_auth_using_pregenerated_jwt", client_id=Config.IIFL_CLIENT_ID)
            return self.session_token

        # Priority 3: Exchange auth code for JWT
        code = auth_code or Config.IIFL_AUTH_CODE
        if not code:
            raise ValueError("No auth_code available for authentication")

        checksum = self._compute_checksum(code)

        # Per IIFL docs and working BridgePy implementation:
        # only send checkSum in the body
        payload = {
            "checkSum": checksum,
        }

        # Match the headers from the proven working iifl_client.py
        headers = {
            "Content-Type": "application/json",
            "AppName": "BridgePy",
            "AppVer": "1.0.0",
            "OsName": "Linux",
        }

        log.info("iifl_auth_attempt", client_id=Config.IIFL_CLIENT_ID)

        resp = await self._http_client.post(
            "/getusersession", json=payload, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()

        # IIFL returns {"status": "Ok", "userSession": "eyJ..."} on success
        status = (data.get("status") or data.get("stat") or "").lower()
        if status == "ok" or data.get("userSession"):
            self.session_token = (
                data.get("userSession")
                or data.get("data", {}).get("userSession")
            )
            if self.session_token:
                log.info("iifl_auth_success", client_id=Config.IIFL_CLIENT_ID)
                return self.session_token
            else:
                raise RuntimeError(f"No userSession in response: {data}")
        else:
            error_msg = data.get("emsg") or data.get("message") or "Unknown auth error"
            log.error("iifl_auth_failed", error=error_msg, response=data)
            raise RuntimeError(f"IIFL authentication failed: {error_msg}")

    def get_auth_headers(self) -> dict:
        """Get headers with Bearer token for API requests."""
        if not self.session_token:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return {
            "Authorization": f"Bearer {self.session_token}",
            "Content-Type": "application/json",
        }

    async def market_request(self, endpoint: str, payload: dict) -> dict:
        """Make an authenticated request to IIFL Capital Markets API.

        Args:
            endpoint: API endpoint path (e.g., '/marketdata/marketquotes')
            payload: Request body dict

        Returns:
            Response JSON dict
        """
        headers = self.get_auth_headers()
        resp = await self._http_client.post(endpoint, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        """Close HTTP client."""
        await self._http_client.aclose()
