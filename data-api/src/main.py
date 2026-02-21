"""NSE Market Data Distribution API.

FastAPI application serving real-time and historical NSE market data
from TimescaleDB. Supports REST JSON endpoints and SSE streaming.
"""
import asyncio
import os
import uuid
import hashlib
import asyncpg
import orjson
import structlog
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

def to_ist(dt) -> str:
    """Convert a datetime to IST string 'YYYY-MM-DD HH:MM:SS'."""
    if dt is None:
        return ''
    if hasattr(dt, 'astimezone'):
        return dt.astimezone(IST).strftime('%Y-%m-%d %H:%M:%S')
    return str(dt)

from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

log = structlog.get_logger()
IST = timezone(timedelta(hours=5, minutes=30))

# Config
DB_HOST = os.getenv("DB_HOST", "nse-timescaledb")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "nseadmin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "nse_market_data")
API_KEY = os.getenv("API_KEY", "")
API_PORT = int(os.getenv("API_PORT", "8088"))

# IIFL Auth config (for web-based auth code flow)
IIFL_APP_KEY = os.getenv("IIFL_APP_KEY", "")
IIFL_CLIENT_ID = os.getenv("IIFL_CLIENT_ID", "")
IIFL_API_SECRET = os.getenv("IIFL_API_SECRET", "")
IIFL_AUTH_REDIRECT = os.getenv("IIFL_AUTH_REDIRECT", "")
IIFL_LOGIN_URL = "https://markets.iiflcapital.com/"
IIFL_SESSION_URL = "https://api.iiflcapital.com/v1/getusersession"
# Simple PIN to protect the /auth page (prevents unauthorized restarts)
AUTH_PAGE_PIN = os.getenv("AUTH_PAGE_PIN", "1234")

# Global DB pool
db_pool: Optional[asyncpg.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: manage DB pool."""
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME,
        min_size=2, max_size=10,
    )
    log.info("api_started", db=DB_NAME)
    yield
    if db_pool:
        await db_pool.close()
    log.info("api_stopped")


app = FastAPI(
    title="NSE Market Data API",
    description="Real-time NSE market data from IIFL Capital Markets",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for Excel/Sheets
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type"],
)

# Serve dashboard static files
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ===== Root: Serve Dashboard =====
@app.get("/", include_in_schema=False)
async def root():
    """Serve the option chain dashboard."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "NSE Market Data API", "docs": "/docs"}


# ===== Auth Middleware =====
async def verify_api_key(request: Request):
    """Verify X-API-Key header or query parameter (`api_key`).

    EventSource (EventSource API) does not support custom headers, so
    callers may pass the API key via query string for SSE connections.
    """
    if not API_KEY:
        return  # No key configured, allow all

    # Prefer header, but fall back to `api_key` query param (useful for SSE)
    key = request.headers.get("X-API-Key", "") or request.query_params.get("api_key", "")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def orjson_response(data: dict, status: int = 200) -> JSONResponse:
    """Fast JSON response using orjson."""
    return JSONResponse(
        content=data,
        status_code=status,
        media_type="application/json",
    )


# ===== Health Check =====
@app.get("/health")
async def health():
    """Health check endpoint."""
    db_ok = False
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            db_ok = True
        except Exception:
            pass

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "timestamp": datetime.now(IST).isoformat(),
    }


# ===== Quotes =====
@app.get("/api/v1/quotes/{symbol}", dependencies=[Depends(verify_api_key)])
async def get_quote(symbol: str):
    """Get latest quote for a symbol (e.g., NIFTY)."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (i.instrument_id)
                i.instrument_id, i.symbol, i.name, i.exchange,
                i.instrument_type, i.strike_price, i.option_type, i.expiry_date,
                t.ltp, t.open_price, t.high_price, t.low_price, t.close_price,
                t.total_traded_volume, t.best_bid_price, t.best_ask_price, t.time
            FROM instruments i
            LEFT JOIN LATERAL (
                SELECT * FROM tick_data td
                WHERE td.instrument_id = i.instrument_id
                ORDER BY td.time DESC LIMIT 1
            ) t ON true
            WHERE UPPER(i.symbol) = UPPER($1)
            AND i.is_active = true
            AND i.option_type IS NULL
            ORDER BY i.instrument_id, t.time DESC
            LIMIT 5
        """, symbol)

    if not rows:
        raise HTTPException(status_code=404, detail=f"No data for {symbol}")

    results = []
    for r in rows:
        results.append({
            "instrument_id": r["instrument_id"],
            "symbol": r["symbol"],
            "name": r["name"],
            "ltp": float(r["ltp"]) if r["ltp"] else None,
            "open": float(r["open_price"]) if r["open_price"] else None,
            "high": float(r["high_price"]) if r["high_price"] else None,
            "low": float(r["low_price"]) if r["low_price"] else None,
            "close": float(r["close_price"]) if r["close_price"] else None,
            "volume": r["total_traded_volume"],
            "bid": float(r["best_bid_price"]) if r["best_bid_price"] else None,
            "ask": float(r["best_ask_price"]) if r["best_ask_price"] else None,
            "timestamp": r["time"].isoformat() if r["time"] else None,
        })

    return {"status": "success", "data": results[0] if len(results) == 1 else results}


# ===== Bulk Quotes =====
@app.get("/api/v1/quotes", dependencies=[Depends(verify_api_key)])
async def get_bulk_quotes(symbols: str = Query(..., description="Comma-separated symbols")):
    """Get latest quotes for multiple symbols."""
    symbol_list = [s.strip().upper() for s in symbols.split(",")]

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (i.instrument_id)
                i.instrument_id, i.symbol, i.name,
                t.ltp, t.open_price, t.high_price, t.low_price, t.close_price,
                t.total_traded_volume, t.best_bid_price, t.best_ask_price, t.time
            FROM instruments i
            LEFT JOIN LATERAL (
                SELECT * FROM tick_data td
                WHERE td.instrument_id = i.instrument_id
                ORDER BY td.time DESC LIMIT 1
            ) t ON true
            WHERE UPPER(i.symbol) = ANY($1::text[])
            AND i.is_active = true
            ORDER BY i.instrument_id, t.time DESC
        """, symbol_list)

    results = []
    for r in rows:
        results.append({
            "instrument_id": r["instrument_id"],
            "symbol": r["symbol"],
            "ltp": float(r["ltp"]) if r["ltp"] else None,
            "volume": r["total_traded_volume"],
            "timestamp": r["time"].isoformat() if r["time"] else None,
        })

    return {"status": "success", "data": results}


# ===== Available Expiries =====
@app.get("/api/v1/expiries/{symbol}", dependencies=[Depends(verify_api_key)])
async def get_expiries(symbol: str):
    """List available expiry dates for a symbol's options."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT expiry_date
            FROM instruments
            WHERE UPPER(symbol) = UPPER($1)
              AND option_type IS NOT NULL
              AND is_active = true
              AND expiry_date >= CURRENT_DATE
            ORDER BY expiry_date
            LIMIT 10
        """, symbol)

    expiries = [str(r["expiry_date"]) for r in rows]
    return {"status": "success", "data": expiries}


# ===== Option Chain =====
@app.get("/api/v1/chain/{symbol}", dependencies=[Depends(verify_api_key)])
async def get_option_chain(
    symbol: str,
    expiry: Optional[str] = Query(None, description="Expiry date YYYY-MM-DD"),
):
    """Get full option chain for a symbol with latest tick + OI data."""
    async with db_pool.acquire() as conn:
        # Get underlying LTP
        underlying = await conn.fetchrow("""
            SELECT t.ltp, t.time FROM tick_data t
            JOIN instruments i ON i.instrument_id = t.instrument_id
            WHERE UPPER(i.symbol) = UPPER($1)
            AND (i.option_type IS NULL OR i.instrument_type = 'INDEX')
            ORDER BY t.time DESC LIMIT 1
        """, symbol)

        # Build option chain query
        query = """
            SELECT
                i.instrument_id, i.strike_price, i.option_type, i.expiry_date,
                i.lot_size,
                t.ltp, t.total_traded_volume AS volume,
                t.best_bid_price AS bid, t.best_ask_price AS ask,
                t.open_price, t.high_price, t.low_price, t.close_price,
                t.time,
                o.open_interest, o.change_in_oi
            FROM instruments i
            LEFT JOIN LATERAL (
                SELECT * FROM tick_data td
                WHERE td.instrument_id = i.instrument_id
                ORDER BY td.time DESC LIMIT 1
            ) t ON true
            LEFT JOIN LATERAL (
                SELECT * FROM oi_data od
                WHERE od.instrument_id = i.instrument_id
                ORDER BY od.time DESC LIMIT 1
            ) o ON true
            WHERE UPPER(i.symbol) = UPPER($1)
            AND i.is_active = true
            AND i.option_type IS NOT NULL
        """
        params = [symbol]

        if expiry:
            # expiry arrives as a string 'YYYY-MM-DD' from the query param —
            # convert to a Python date so asyncpg binds it correctly.
            try:
                expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid expiry format, expected YYYY-MM-DD")

            query += " AND i.expiry_date = $2"
            params.append(expiry_date)
        else:
            # Default: nearest expiry
            query += " AND i.expiry_date = (SELECT MIN(expiry_date) FROM instruments WHERE UPPER(symbol) = UPPER($1) AND expiry_date >= CURRENT_DATE AND option_type IS NOT NULL AND is_active = true)"

        query += " ORDER BY i.strike_price, i.option_type"

        rows = await conn.fetch(query, *params)

    # Build chain structure
    chain = {}
    total_ce_oi = 0
    total_pe_oi = 0

    for r in rows:
        strike = float(r["strike_price"])
        opt_type = r["option_type"]

        if strike not in chain:
            chain[strike] = {"strike": strike, "ce": None, "pe": None}

        opt_data = {
            "instrument_id": r["instrument_id"],
            "ltp": float(r["ltp"]) if r["ltp"] else 0,
            "bid": float(r["bid"]) if r["bid"] else 0,
            "ask": float(r["ask"]) if r["ask"] else 0,
            "open": float(r["open_price"]) if r["open_price"] else 0,
            "high": float(r["high_price"]) if r["high_price"] else 0,
            "low": float(r["low_price"]) if r["low_price"] else 0,
            "close": float(r["close_price"]) if r["close_price"] else 0,
            "volume": r["volume"] or 0,
            "oi": r["open_interest"] or 0,
            "change_oi": r["change_in_oi"] or 0,
            "lot_size": r["lot_size"] or 0,
            "last_updated": r["time"].isoformat() if r["time"] else None,
        }

        if opt_type == "CE":
            chain[strike]["ce"] = opt_data
            total_ce_oi += opt_data["oi"]
        elif opt_type == "PE":
            chain[strike]["pe"] = opt_data
            total_pe_oi += opt_data["oi"]

    sorted_chain = sorted(chain.values(), key=lambda x: x["strike"])
    pcr = round(total_pe_oi / total_ce_oi, 4) if total_ce_oi > 0 else 0

    expiry_date = str(rows[0]["expiry_date"]) if rows else None

    return {
        "status": "success",
        "data": {
            "symbol": symbol.upper(),
            "underlying_ltp": float(underlying["ltp"]) if underlying and underlying["ltp"] else None,
            "expiry": expiry_date,
            "timestamp": datetime.now(IST).isoformat(),
            "chain": sorted_chain,
            "totals": {
                "total_ce_oi": total_ce_oi,
                "total_pe_oi": total_pe_oi,
                "pcr": pcr,
            },
        },
    }


# ===== OHLCV Candles =====
@app.get("/api/v1/ohlcv/{symbol}", dependencies=[Depends(verify_api_key)])
async def get_ohlcv(
    symbol: str,
    instrument_id: Optional[int] = None,
    interval: str = Query("1m", description="1m, 5m, 15m, 1d"),
    limit: int = Query(100, le=1000),
):
    """Get OHLCV candle data."""
    async with db_pool.acquire() as conn:
        if instrument_id:
            iid = instrument_id
        else:
            # Prefer INDEX instrument for underlying spot price history
            row = await conn.fetchrow(
                """SELECT instrument_id FROM instruments
                   WHERE UPPER(symbol) = UPPER($1) AND is_active = true
                   ORDER BY CASE WHEN instrument_type = 'INDEX' THEN 0 ELSE 1 END
                   LIMIT 1""",
                symbol
            )
            if not row:
                raise HTTPException(404, f"Instrument not found: {symbol}")
            iid = row["instrument_id"]

        if interval == "1m":
            rows = await conn.fetch("""
                SELECT bucket AS time, open_price, high_price, low_price, close_price, volume
                FROM ohlcv_1min
                WHERE instrument_id = $1
                ORDER BY bucket DESC LIMIT $2
            """, iid, limit)
        else:
            rows = await conn.fetch("""
                SELECT time, open_price, high_price, low_price, close_price, volume
                FROM historical_ohlcv
                WHERE instrument_id = $1 AND interval_type = $2
                ORDER BY time DESC LIMIT $3
            """, iid, interval, limit)

    candles = [{
        "time": to_ist(r["time"]),
        "open": float(r["open_price"]) if r["open_price"] else 0,
        "high": float(r["high_price"]) if r["high_price"] else 0,
        "low": float(r["low_price"]) if r["low_price"] else 0,
        "close": float(r["close_price"]) if r["close_price"] else 0,
        "volume": r["volume"] or 0,
    } for r in rows]

    return {"status": "success", "data": {"symbol": symbol, "interval": interval, "candles": candles}}


# ===== Chain Snapshots for Backtesting =====
@app.get("/api/v1/chain-history/{symbol}", dependencies=[Depends(verify_api_key)])
async def get_chain_history(
    symbol: str,
    expiry: Optional[str] = Query(None, description="Expiry date YYYY-MM-DD"),
    from_time: Optional[str] = Query(None, alias="from", description="Start time ISO8601"),
    to_time: Optional[str] = Query(None, alias="to", description="End time ISO8601"),
    interval_sec: int = Query(3, description="Snapshot interval in seconds (min 2)"),
    limit: int = Query(5000, le=50000),
):
    """Get historical option chain snapshots for backtesting.

    Returns tick-by-tick snapshots of the full option chain at the
    specified interval. Each row is one instrument at one point in time.
    """
    async with db_pool.acquire() as conn:
        # Determine expiry
        if expiry:
            try:
                expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(400, "Invalid expiry format, expected YYYY-MM-DD")
        else:
            expiry_date = await conn.fetchval("""
                SELECT MIN(expiry_date) FROM instruments
                WHERE UPPER(symbol) = UPPER($1) AND expiry_date >= CURRENT_DATE
                AND option_type IS NOT NULL AND is_active = true
            """, symbol)
            if not expiry_date:
                raise HTTPException(404, f"No active options found for {symbol}")

        # Get instrument IDs for this expiry (options + index)
        inst_rows = await conn.fetch("""
            SELECT instrument_id, strike_price, option_type, instrument_type
            FROM instruments
            WHERE UPPER(symbol) = UPPER($1) AND is_active = true
            AND (
                (option_type IS NOT NULL AND expiry_date = $2)
                OR instrument_type = 'INDEX'
            )
        """, symbol, expiry_date)

        if not inst_rows:
            raise HTTPException(404, "No instruments found")

        # Filter to 15 strikes above + ATM + 15 strikes below
        # First, get the current NIFTY spot to determine ATM
        spot_row = await conn.fetchrow("""
            SELECT ltp FROM tick_data
            WHERE instrument_id = (
                SELECT instrument_id FROM instruments
                WHERE UPPER(symbol) = UPPER($1) AND instrument_type = 'INDEX'
                AND is_active = true LIMIT 1
            )
            ORDER BY time DESC LIMIT 1
        """, symbol)
        spot_price = float(spot_row["ltp"]) if spot_row and spot_row["ltp"] else 0
        atm_strike = round(spot_price / 50) * 50  # NIFTY rounds to 50

        # Get sorted unique strikes and find the ATM window
        all_strikes = sorted(set(
            float(r["strike_price"]) for r in inst_rows
            if r["strike_price"] is not None
        ))
        if all_strikes and atm_strike > 0:
            # Find closest strike to ATM
            atm_idx = min(range(len(all_strikes)),
                          key=lambda i: abs(all_strikes[i] - atm_strike))
            start_idx = max(0, atm_idx - 15)
            end_idx = min(len(all_strikes) - 1, atm_idx + 15)
            allowed_strikes = set(all_strikes[start_idx:end_idx + 1])

            # Filter instruments to allowed strikes + INDEX
            inst_rows = [
                r for r in inst_rows
                if r["instrument_type"] == 'INDEX'
                or (r["strike_price"] is not None
                    and float(r["strike_price"]) in allowed_strikes)
            ]

        inst_ids = [r["instrument_id"] for r in inst_rows]
        inst_map = {r["instrument_id"]: r for r in inst_rows}

        # Build time-sampled query using time_bucket
        bucket_interval = f"{max(interval_sec, 2)} seconds"
        query = f"""
            SELECT time_bucket('{bucket_interval}', time) AS bucket,
                   instrument_id,
                   last(ltp, time) AS ltp,
                   last(total_traded_volume, time) AS volume,
                   last(best_bid_price, time) AS bid,
                   last(best_ask_price, time) AS ask,
                   last(open_price, time) AS open_price,
                   last(high_price, time) AS high_price,
                   last(low_price, time) AS low_price,
                   last(close_price, time) AS close_price
            FROM tick_data
            WHERE instrument_id = ANY($1::bigint[])
        """
        params: list = [inst_ids]

        if from_time:
            query += f" AND time >= ${len(params)+1}::timestamptz"
            params.append(from_time)
        if to_time:
            query += f" AND time <= ${len(params)+1}::timestamptz"
            params.append(to_time)

        query += f" GROUP BY bucket, instrument_id ORDER BY bucket, instrument_id LIMIT ${len(params)+1}"
        params.append(limit)

        rows = await conn.fetch(query, *params)

    snapshots = []
    for r in rows:
        iid = r["instrument_id"]
        meta = inst_map.get(iid, {})
        snapshots.append({
            "time": to_ist(r["bucket"]),
            "instrument_id": iid,
            "strike": float(meta["strike_price"]) if meta.get("strike_price") else None,
            "option_type": meta.get("option_type"),
            "type": meta.get("instrument_type"),
            "ltp": float(r["ltp"]) if r["ltp"] else 0,
            "bid": float(r["bid"]) if r["bid"] else 0,
            "ask": float(r["ask"]) if r["ask"] else 0,
            "volume": r["volume"] or 0,
            "open": float(r["open_price"]) if r["open_price"] else 0,
            "high": float(r["high_price"]) if r["high_price"] else 0,
            "low": float(r["low_price"]) if r["low_price"] else 0,
            "close": float(r["close_price"]) if r["close_price"] else 0,
        })

    return {
        "status": "success",
        "data": {
            "symbol": symbol.upper(),
            "expiry": str(expiry_date),
            "interval_sec": interval_sec,
            "rows": len(snapshots),
            "snapshots": snapshots,
        },
    }


# ===== OI Data =====
@app.get("/api/v1/oi/{symbol}", dependencies=[Depends(verify_api_key)])
async def get_oi(symbol: str, limit: int = Query(50, le=500)):
    """Get open interest data for options of a symbol."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT i.instrument_id, i.strike_price, i.option_type, i.expiry_date,
                   o.open_interest, o.change_in_oi, o.ltp, o.volume, o.time
            FROM instruments i
            LEFT JOIN LATERAL (
                SELECT * FROM oi_data od
                WHERE od.instrument_id = i.instrument_id
                ORDER BY od.time DESC LIMIT 1
            ) o ON true
            WHERE UPPER(i.symbol) = UPPER($1)
            AND i.is_active = true
            AND i.option_type IS NOT NULL
            ORDER BY i.strike_price, i.option_type
            LIMIT $2
        """, symbol, limit)

    data = [{
        "strike": float(r["strike_price"]),
        "option_type": r["option_type"],
        "expiry": str(r["expiry_date"]),
        "oi": r["open_interest"] or 0,
        "change_oi": r["change_in_oi"] or 0,
        "ltp": float(r["ltp"]) if r["ltp"] else 0,
        "volume": r["volume"] or 0,
        "timestamp": r["time"].isoformat() if r["time"] else None,
    } for r in rows]

    return {"status": "success", "data": data}


# ===== Tick history (debug) =====
@app.get("/api/v1/ticks/{instrument_id}", dependencies=[Depends(verify_api_key)])
async def get_ticks(instrument_id: int, limit: int = Query(100, le=1000)):
    """Return recent tick rows for a specific instrument_id (debugging).

    Useful for verifying whether the ingest layer is writing correct instrument_id
    values and for comparing against live MQTT messages.
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT time, ltp, ltq, total_traded_volume, best_bid_price, best_bid_qty,
                   best_ask_price, best_ask_qty
            FROM tick_data
            WHERE instrument_id = $1
            ORDER BY time DESC LIMIT $2
            """,
            instrument_id, limit,
        )

    data = [{
        "time": r["time"].isoformat() if r["time"] else None,
        "ltp": float(r["ltp"]) if r["ltp"] else None,
        "ltq": int(r["ltq"]) if r["ltq"] else None,
        "volume": int(r["total_traded_volume"]) if r["total_traded_volume"] else 0,
        "bid": float(r["best_bid_price"]) if r["best_bid_price"] else None,
        "bid_qty": int(r["best_bid_qty"]) if r["best_bid_qty"] else None,
        "ask": float(r["best_ask_price"]) if r["best_ask_price"] else None,
        "ask_qty": int(r["best_ask_qty"]) if r["best_ask_qty"] else None,
    } for r in rows]

    return {"status": "success", "data": data}


# ===== Historical Data (for backtesting) =====
@app.get("/api/v1/historical/{symbol}", dependencies=[Depends(verify_api_key)])
async def get_historical(
    symbol: str,
    instrument_id: Optional[int] = None,
    interval: str = Query("1d"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    limit: int = Query(500, le=5000),
):
    """Get historical OHLCV data for backtesting."""
    async with db_pool.acquire() as conn:
        if instrument_id:
            iid = instrument_id
        else:
            row = await conn.fetchrow(
                "SELECT instrument_id FROM instruments WHERE UPPER(symbol) = UPPER($1) AND is_active = true LIMIT 1",
                symbol
            )
            if not row:
                raise HTTPException(404, f"Instrument not found: {symbol}")
            iid = row["instrument_id"]

        query = """
            SELECT time, open_price, high_price, low_price, close_price, volume, oi
            FROM historical_ohlcv
            WHERE instrument_id = $1 AND interval_type = $2
        """
        params = [iid, interval]

        if from_date:
            query += f" AND time >= ${len(params)+1}::timestamptz"
            params.append(from_date)
        if to_date:
            query += f" AND time <= ${len(params)+1}::timestamptz"
            params.append(to_date)

        query += f" ORDER BY time ASC LIMIT ${len(params)+1}"
        params.append(limit)

        rows = await conn.fetch(query, *params)

    candles = [{
        "time": r["time"].isoformat(),
        "open": float(r["open_price"]) if r["open_price"] else 0,
        "high": float(r["high_price"]) if r["high_price"] else 0,
        "low": float(r["low_price"]) if r["low_price"] else 0,
        "close": float(r["close_price"]) if r["close_price"] else 0,
        "volume": r["volume"] or 0,
        "oi": r["oi"] or 0,
    } for r in rows]

    return {"status": "success", "data": {"symbol": symbol, "interval": interval, "candles": candles}}


# ===== Instruments =====
@app.get("/api/v1/instruments", dependencies=[Depends(verify_api_key)])
async def get_instruments(
    symbol: Optional[str] = None,
    instrument_type: Optional[str] = None,
    active_only: bool = True,
):
    """List available instruments."""
    async with db_pool.acquire() as conn:
        query = "SELECT * FROM instruments WHERE 1=1"
        params = []

        if active_only:
            query += " AND is_active = true"
        if symbol:
            params.append(symbol)
            query += f" AND UPPER(symbol) = UPPER(${len(params)})"
        if instrument_type:
            params.append(instrument_type)
            query += f" AND UPPER(instrument_type) = UPPER(${len(params)})"

        query += " ORDER BY symbol, expiry_date, strike_price LIMIT 500"
        rows = await conn.fetch(query, *params)

    data = [{
        "instrument_id": r["instrument_id"],
        "symbol": r["symbol"],
        "name": r["name"],
        "instrument_type": r["instrument_type"],
        "expiry_date": str(r["expiry_date"]) if r["expiry_date"] else None,
        "strike_price": float(r["strike_price"]) if r["strike_price"] else None,
        "option_type": r["option_type"],
        "lot_size": r["lot_size"],
    } for r in rows]

    return {"status": "success", "data": data, "count": len(data)}


# ===== SSE Real-Time Stream =====
@app.get("/api/v1/stream", dependencies=[Depends(verify_api_key)])
async def sse_stream(request: Request, symbols: Optional[str] = None):
    """Server-Sent Events stream for real-time market data.

    Connect to this endpoint and receive continuous updates.
    Supports filtering by comma-separated symbols.
    """
    # For the SSE stream, we poll the database for latest ticks
    # In production, this would use the in-memory broadcaster
    filter_symbols = [s.strip().upper() for s in symbols.split(",")] if symbols else None

    async def event_generator():
        last_seen = {}
        while True:
            if await request.is_disconnected():
                break

            try:
                async with db_pool.acquire() as conn:
                    query = """
                        SELECT DISTINCT ON (i.instrument_id)
                            i.instrument_id, i.symbol, i.strike_price, i.option_type,
                            t.ltp, t.total_traded_volume, t.best_bid_price, t.best_ask_price, t.time
                        FROM instruments i
                        LEFT JOIN LATERAL (
                            SELECT * FROM tick_data td
                            WHERE td.instrument_id = i.instrument_id
                            ORDER BY td.time DESC LIMIT 1
                        ) t ON true
                        WHERE i.is_active = true
                    """
                    params = []
                    if filter_symbols:
                        params.append(filter_symbols)
                        query += f" AND UPPER(i.symbol) = ANY(${len(params)}::text[])"

                    query += " ORDER BY i.instrument_id, t.time DESC"
                    rows = await conn.fetch(query, *params)

                for r in rows:
                    iid = r["instrument_id"]
                    current_time = r["time"]

                    # Only send if data changed
                    if current_time and (iid not in last_seen or last_seen[iid] != current_time):
                        last_seen[iid] = current_time
                        tick_data = {
                            "instrument_id": iid,
                            "symbol": r["symbol"],
                            "strike": float(r["strike_price"]) if r["strike_price"] else None,
                            "option_type": r["option_type"],
                            "ltp": float(r["ltp"]) if r["ltp"] else None,
                            "volume": r["total_traded_volume"],
                            "bid": float(r["best_bid_price"]) if r["best_bid_price"] else None,
                            "ask": float(r["best_ask_price"]) if r["best_ask_price"] else None,
                            "timestamp": current_time.isoformat(),
                        }
                        yield {"event": "tick", "data": orjson.dumps(tick_data).decode()}

            except Exception as e:
                log.error("sse_error", error=str(e))

            await asyncio.sleep(0.5)  # Poll every 500ms

    return EventSourceResponse(event_generator())


# ===== Auth: Web-based IIFL Login =====
# Flow: /auth → IIFL login → /auth/callback?code=XXX → exchange for JWT → store in DB

AUTH_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE Ingestor — Auth Update</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0d1117;color:#c9d1d9;
  display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;max-width:420px;width:100%;text-align:center}
h1{font-size:1.4em;margin-bottom:8px;color:#58a6ff}
p{color:#8b949e;margin:12px 0;font-size:0.95em;line-height:1.5}
.pin-input{width:120px;padding:12px;font-size:1.3em;text-align:center;background:#0d1117;
  border:1px solid #30363d;border-radius:8px;color:#c9d1d9;letter-spacing:8px;margin:16px auto}
.btn{display:inline-block;padding:12px 32px;background:#238636;color:#fff;border:none;
  border-radius:8px;font-size:1em;cursor:pointer;text-decoration:none;margin-top:16px}
.btn:hover{background:#2ea043}
.status{margin-top:16px;padding:12px;border-radius:8px;font-size:0.9em}
.success{background:#0d2818;border:1px solid #238636;color:#3fb950}
.error{background:#2d1117;border:1px solid #da3633;color:#f85149}
.info{background:#0d1d31;border:1px solid #1f6feb;color:#58a6ff}
</style>
</head>
<body>
<div class="card">
  <h1>🔑 NSE Ingestor Auth</h1>
  <p>Enter PIN to start broker login.<br>After login, the session will be updated automatically.</p>
  <form method="GET" action="/auth/login">
    <input type="password" name="pin" class="pin-input" maxlength="6" placeholder="PIN" required autofocus>
    <br><button type="submit" class="btn">Login →</button>
  </form>
  STATUSPLACEHOLDER
</div>
</body></html>"""

AUTH_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auth Updated!</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0d1117;color:#c9d1d9;
  display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;max-width:500px;width:100%;text-align:center}
h1{font-size:1.4em;margin-bottom:12px}
.success{color:#3fb950} .error{color:#f85149}
p{color:#8b949e;margin:8px 0;font-size:0.95em;line-height:1.5}
.token{font-family:monospace;font-size:0.75em;color:#8b949e;background:#0d1117;padding:8px;border-radius:6px;
  word-break:break-all;margin:12px 0}
a{color:#58a6ff;text-decoration:none}
</style>
</head>
<body>
<div class="card">CONTENT</div>
</body></html>"""


@app.get("/auth", include_in_schema=False)
async def auth_page(status: str = ""):
    """Auth page with PIN entry."""
    from fastapi.responses import HTMLResponse
    status_html = ""
    if status == "success":
        status_html = '<div class="status success">✅ Session updated! Ingestor will use it on next reconnect.</div>'
    elif status == "pin_error":
        status_html = '<div class="status error">❌ Wrong PIN. Try again.</div>'
    elif status == "auth_error":
        status_html = '<div class="status error">❌ Broker auth failed. Try again.</div>'
    elif status == "config_error":
        status_html = '<div class="status error">❌ Broker credentials not configured on server.</div>'

    html = AUTH_PAGE_HTML.replace("STATUSPLACEHOLDER", status_html)
    return HTMLResponse(html)


@app.get("/auth/login", include_in_schema=False)
async def auth_login(pin: str = ""):
    """Verify PIN and redirect to IIFL login."""
    from fastapi.responses import RedirectResponse

    if pin != AUTH_PAGE_PIN:
        return RedirectResponse("/auth?status=pin_error", status_code=302)

    if not IIFL_APP_KEY:
        return RedirectResponse("/auth?status=config_error", status_code=302)

    # Redirect to IIFL login page
    iifl_url = f"{IIFL_LOGIN_URL}?v=1&appkey={IIFL_APP_KEY}&redirecturl={IIFL_AUTH_REDIRECT}"
    return RedirectResponse(iifl_url, status_code=302)


@app.get("/auth/callback", include_in_schema=False)
async def auth_callback(request: Request):
    """IIFL redirects here after login with ?authcode=XXX&clientid=YYY.
    Exchange code for JWT and store in DB for ingestor to pick up."""
    from fastapi.responses import HTMLResponse

    # IIFL uses 'authcode' param (not 'code')
    code = request.query_params.get("authcode") or request.query_params.get("code") or ""

    if not code:
        content = '<h1 class="error">❌ No auth code received</h1><p>IIFL did not return an auth code.</p><p><a href="/auth">← Try again</a></p>'
        return HTMLResponse(AUTH_SUCCESS_HTML.replace("CONTENT", content))

    if not IIFL_CLIENT_ID or not IIFL_API_SECRET:
        content = '<h1 class="error">❌ Server not configured</h1><p>IIFL credentials missing.</p>'
        return HTMLResponse(AUTH_SUCCESS_HTML.replace("CONTENT", content))

    # Exchange auth code for JWT
    try:
        raw = f"{IIFL_CLIENT_ID}{code}{IIFL_API_SECRET}"
        checksum = hashlib.sha256(raw.encode()).hexdigest()

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(IIFL_SESSION_URL, json={"checkSum": checksum}, headers={
                "Content-Type": "application/json",
                "AppName": "BridgePy",
                "AppVer": "1.0.0",
                "OsName": "Linux",
            })
            resp.raise_for_status()
            data = resp.json()

        jwt_token = data.get("userSession") or data.get("data", {}).get("userSession")
        if not jwt_token:
            error_msg = data.get("emsg") or data.get("message") or str(data)
            log.error("auth_callback_failed", error=error_msg)
            content = f'<h1 class="error">❌ Auth Failed</h1><p>{error_msg}</p><p><a href="/auth">← Try again</a></p>'
            return HTMLResponse(AUTH_SUCCESS_HTML.replace("CONTENT", content))

        # Store JWT in database for ingestor to pick up
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES ('iifl_session_token', $1, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value = $1, updated_at = NOW()
            """, jwt_token)
            # Also store the auth code
            await conn.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES ('iifl_auth_code', $1, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value = $1, updated_at = NOW()
            """, code)

        log.info("auth_callback_success", jwt_len=len(jwt_token))

        content = f'''<h1 class="success">✅ Auth Updated Successfully!</h1>
        <p>Session has been saved. The ingestor will pick it up automatically.</p>
        <p style="color:#3fb950; margin-top:12px">✓ New session token acquired</p>
        <p style="margin-top:16px"><a href="/">← Back to Dashboard</a></p>'''
        return HTMLResponse(AUTH_SUCCESS_HTML.replace("CONTENT", content))

    except Exception as e:
        log.error("auth_callback_error", error=str(e))
        content = f'<h1 class="error">❌ Error</h1><p>{str(e)}</p><p><a href="/auth">← Try again</a></p>'
        return HTMLResponse(AUTH_SUCCESS_HTML.replace("CONTENT", content))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
