"""Configuration loaded from environment variables."""
import os


class Config:
    # Database
    DB_HOST = os.getenv("DB_HOST", "nse-timescaledb")
    DB_PORT = int(os.getenv("DB_PORT", "5432"))
    DB_USER = os.getenv("DB_USER", "nseadmin")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_NAME = os.getenv("DB_NAME", "nse_market_data")

    # IIFL Capital Markets API
    IIFL_BASE_URL = "https://api.iiflcapital.com/v1"
    IIFL_WS_HOST = "bridge.iiflcapital.com"
    IIFL_WS_PORT = 8883
    IIFL_APP_KEY = os.getenv("IIFL_APP_KEY", "")
    IIFL_API_SECRET = os.getenv("IIFL_API_SECRET", "")
    IIFL_CLIENT_ID = os.getenv("IIFL_CLIENT_ID", "")
    IIFL_AUTH_CODE = os.getenv("IIFL_AUTH_CODE", "")
    IIFL_SESSION_TOKEN = os.getenv("IIFL_SESSION_TOKEN", "")  # Pre-generated JWT (skips auth code exchange)
    IIFL_REDIRECT_URL = os.getenv("IIFL_REDIRECT_URL", "http://localhost:3000/callback")

    # Subscription
    SUBSCRIPTION_SYMBOL = os.getenv("SUBSCRIPTION_SYMBOL", "NIFTY")
    SUBSCRIPTION_SEGMENT = os.getenv("SUBSCRIPTION_SEGMENT", "NSEFO")
    STRIKE_RANGE = int(os.getenv("STRIKE_RANGE", "20"))

    # Backfill
    ENABLE_BACKFILL = os.getenv("ENABLE_BACKFILL", "false").lower() == "true"
    BACKFILL_DAYS = int(os.getenv("BACKFILL_DAYS", "30"))

    # Batch write settings
    BATCH_SIZE = int(os.getenv("BATCH_SIZE", "100"))
    BATCH_FLUSH_INTERVAL = float(os.getenv("BATCH_FLUSH_INTERVAL", "0.5"))

    # OI polling interval (seconds)
    OI_POLL_INTERVAL = int(os.getenv("OI_POLL_INTERVAL", "180"))

    # Ingest sanity-checks (drop/flag tick if LTP jump exceeds threshold)
    LTP_SANITY_ENABLED = os.getenv("LTP_SANITY_ENABLED", "false").lower() == "true"
    # Percentage threshold (e.g. 500.0 = 500%)
    LTP_SANITY_THRESHOLD_PCT = float(os.getenv("LTP_SANITY_THRESHOLD_PCT", "500.0"))
    # Minimum last-traded-quantity required to accept a large LTP jump
    # (ticks with ltq below this value will be subject to sanity filtering)
    LTP_SANITY_MIN_LTQ = int(os.getenv("LTP_SANITY_MIN_LTQ", "1"))

    # If true, records with unknown instrument_id (not in instruments table)
    # will be dropped at batch-flush time. Default: false (log-only).
    DROP_UNKNOWN_TICKS = os.getenv("DROP_UNKNOWN_TICKS", "false").lower() == "true"

    @classmethod
    def db_dsn(cls):
        return f"postgresql://{cls.DB_USER}:{cls.DB_PASSWORD}@{cls.DB_HOST}:{cls.DB_PORT}/{cls.DB_NAME}"

    @classmethod
    def validate(cls):
        required = ["IIFL_APP_KEY", "IIFL_API_SECRET", "IIFL_CLIENT_ID", "DB_PASSWORD"]
        missing = [k for k in required if not getattr(cls, k)]
        if missing:
            raise ValueError(f"Missing required config: {', '.join(missing)}")
