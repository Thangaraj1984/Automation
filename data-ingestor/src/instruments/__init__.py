"""IIFL Capital Markets Instrument Master Manager.

Downloads and parses daily instrument master files from IIFL:
- https://api.iiflcapital.com/v1/contractfiles/NSEFO.json
- https://api.iiflcapital.com/v1/contractfiles/INDICES.json

Filters for NIFTY weekly options and builds subscription lists.
Also fetches the NIFTY 50 index instrument from the INDICES segment
for spot price tracking (instrument_id=999920000).
"""
import httpx
import asyncpg
import structlog
from datetime import datetime, date, timedelta
from typing import Optional
from ..config import Config

log = structlog.get_logger()

# IIFL contract file URLs
CONTRACT_URLS = {
    "NSEFO": f"{Config.IIFL_BASE_URL}/contractfiles/NSEFO.json",
    "NSEEQ": f"{Config.IIFL_BASE_URL}/contractfiles/NSEEQ.json",
    "INDICES": f"{Config.IIFL_BASE_URL}/contractfiles/INDICES.json",
}

# NIFTY 50 index instrument ID from INDICES contract file
NIFTY_INDEX_INSTRUMENT_ID = 999920000


class InstrumentMaster:
    """Manages IIFL instrument master data and subscription lists."""

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        self._instruments: list[dict] = []
        self._subscriptions: list[str] = []
        self._http = httpx.AsyncClient(timeout=60.0)

    async def download_master(self, segment: str = "NSEFO") -> list[dict]:
        """Download instrument master JSON from IIFL.

        Args:
            segment: Exchange segment (NSEFO, NSEEQ, INDICES, etc.)

        Returns:
            List of instrument dicts from IIFL.
        """
        url = CONTRACT_URLS.get(segment)
        if not url:
            raise ValueError(f"Unknown segment: {segment}")

        log.info("instrument_master_download", segment=segment, url=url)

        resp = await self._http.get(url)
        resp.raise_for_status()
        data = resp.json()

        # IIFL returns list of instruments or wrapped in a data field
        instruments = data if isinstance(data, list) else data.get("data", data.get("instruments", []))
        log.info("instrument_master_downloaded", segment=segment, count=len(instruments))

        self._instruments = instruments
        return instruments

    async def download_index_master(self) -> Optional[dict]:
        """Download INDICES contract file and find NIFTY 50 index instrument.

        Returns:
            NIFTY 50 index instrument dict, or None if not found.
        """
        url = CONTRACT_URLS["INDICES"]
        log.info("index_master_download", url=url)

        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            data = resp.json()

            indices = data if isinstance(data, list) else data.get("data", [])
            log.info("index_master_downloaded", count=len(indices))

            for inst in indices:
                name = (inst.get("formattedInstrumentName", "") or "").upper()
                if name == "NIFTY 50":
                    nifty_index = {
                        "instrument_id": int(inst.get("instrumentId", 0)),
                        "exchange": "NSE",
                        "segment": "INDICES",
                        "symbol": "NIFTY",
                        "name": "NIFTY 50 INDEX",
                        "instrument_type": "INDEX",
                        "expiry_date": None,
                        "strike_price": None,
                        "option_type": None,
                        "lot_size": 0,
                        "tick_size": 0.05,
                    }
                    log.info("nifty_index_found", instrument_id=nifty_index["instrument_id"])
                    return nifty_index

            log.warning("nifty_index_not_found_in_contract_file")
            return None
        except Exception as e:
            log.error("index_master_download_failed", error=str(e))
            return None

    def filter_weekly_options(
        self,
        symbol: str = "NIFTY",
        underlying_ltp: float = 0,
        strike_range: int = 20,
    ) -> list[dict]:
        """Filter instruments for weekly options around ATM.

        Args:
            symbol: Underlying symbol (NIFTY, BANKNIFTY)
            underlying_ltp: Current LTP of the underlying for ATM calc
            strike_range: Number of strikes above and below ATM

        Returns:
            Filtered list of option instruments.
        """
        today = date.today()

        options = []
        for inst in self._instruments:
            underlying = (
                inst.get("underlyingInstrumentSymbol", "")
                or inst.get("underlyingInstrumentName", "")
                or inst.get("symbol", "")
                or inst.get("tradingSymbol", "")
            )
            inst_type = inst.get("instrumentType", "") or inst.get("instrument_type", "")
            expiry_str = inst.get("expiry", "") or inst.get("expiryDate", "")

            if underlying.upper() != symbol.upper():
                continue
            if "OPT" not in inst_type.upper():
                continue

            expiry_date = self._parse_expiry(expiry_str)
            if not expiry_date:
                continue

            days_to_expiry = (expiry_date - today).days
            if days_to_expiry < 0 or days_to_expiry > 14:
                continue

            strike = float(inst.get("strikePrice", 0) or inst.get("strike", 0))
            option_type = inst.get("optionType", "") or inst.get("option_type", "")
            trading_symbol = inst.get("tradingSymbol", "") or inst.get("formattedInstrumentName", "")

            options.append({
                "instrument_id": int(inst.get("instrumentId", 0) or inst.get("token", 0)),
                "exchange": "NSE",
                "segment": "FO",
                "symbol": symbol,
                "name": trading_symbol,
                "instrument_type": inst_type,
                "expiry_date": expiry_date,
                "strike_price": strike,
                "option_type": option_type.upper() if option_type else None,
                "lot_size": int(inst.get("lotSize", 0) or inst.get("lot_size", 0)),
                "tick_size": float(inst.get("tickSize", 0.05) or inst.get("tick_size", 0.05)),
            })

        # Sort by strike
        options.sort(key=lambda x: (x["expiry_date"], x["strike_price"], x.get("option_type", "")))

        # If we have underlying LTP, filter around ATM
        if underlying_ltp > 0 and options:
            step = self._detect_strike_step(options)
            if step > 0:
                atm_strike = round(underlying_ltp / step) * step
                min_strike = atm_strike - (strike_range * step)
                max_strike = atm_strike + (strike_range * step)
                options = [o for o in options if min_strike <= o["strike_price"] <= max_strike]
                log.info("atm_filter", atm=atm_strike, step=step,
                         min_strike=min_strike, max_strike=max_strike,
                         filtered_count=len(options))

        log.info("weekly_options_filtered", symbol=symbol, count=len(options))
        return options

    def _detect_strike_step(self, options: list[dict]) -> float:
        """Detect strike price step from instrument list."""
        strikes = sorted(set(o["strike_price"] for o in options))
        if len(strikes) < 2:
            return 50  # Default NIFTY step
        diffs = [strikes[i+1] - strikes[i] for i in range(min(10, len(strikes)-1))]
        return min(diffs) if diffs else 50

    def _parse_expiry(self, expiry_str: str) -> Optional[date]:
        """Parse expiry date from various IIFL formats.

        IIFL uses formats like:
        - "30-Mar-2026 23:59"
        - "2026-03-30T23:59:00"
        - "30-03-2026"
        - "30-Mar-2026"
        """
        if not expiry_str:
            return None

        # Strip any time component after space or T
        date_part = expiry_str.strip()
        if " " in date_part:
            date_part = date_part.split(" ")[0]  # "30-Mar-2026 23:59" -> "30-Mar-2026"
        elif "T" in date_part:
            date_part = date_part.split("T")[0]  # "2026-03-30T23:59:00" -> "2026-03-30"

        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"):
            try:
                return datetime.strptime(date_part, fmt).date()
            except ValueError:
                continue
        return None

    async def upsert_instruments(self, instruments: list[dict]):
        """Upsert instruments into the database."""
        if not instruments:
            return

        async with self.db_pool.acquire() as conn:
            for inst in instruments:
                await conn.execute("""
                    INSERT INTO instruments (
                        instrument_id, exchange, segment, symbol, name,
                        instrument_type, expiry_date, strike_price, option_type,
                        lot_size, tick_size, is_active, updated_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,true,NOW())
                    ON CONFLICT (instrument_id) DO UPDATE SET
                        symbol = EXCLUDED.symbol,
                        name = EXCLUDED.name,
                        expiry_date = EXCLUDED.expiry_date,
                        strike_price = EXCLUDED.strike_price,
                        option_type = EXCLUDED.option_type,
                        lot_size = EXCLUDED.lot_size,
                        is_active = true,
                        updated_at = NOW()
                """,
                    inst["instrument_id"], inst["exchange"], inst["segment"],
                    inst["symbol"], inst["name"], inst["instrument_type"],
                    inst["expiry_date"], inst["strike_price"], inst.get("option_type"),
                    inst.get("lot_size", 0), inst.get("tick_size", 0.05),
                )

        log.info("instruments_upserted", count=len(instruments))

    async def deactivate_expired(self):
        """Mark instruments with past expiry_date as inactive.

        Runs at startup and periodically to ensure expired options
        don't pollute active queries or cause cross-expiry data mixing.
        """
        async with self.db_pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE instruments
                SET is_active = false, updated_at = NOW()
                WHERE expiry_date < CURRENT_DATE
                  AND option_type IS NOT NULL
                  AND is_active = true
            """)
            # result is like "UPDATE 42"
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                log.info("expired_instruments_deactivated", count=count)

    def build_subscription_list(self, instruments: list[dict]) -> list[str]:
        """Build IIFL WebSocket subscription list.

        Format: ["nsefo/{instrumentId}", ...]
        Maps segments: FO->nsefo, EQ->nseeq, INDICES->nseeq
        """
        segment_map = {"FO": "nsefo", "EQ": "nseeq", "CURR": "nsecurr", "INDICES": "nseeq"}
        subs = []
        for inst in instruments:
            seg = segment_map.get(inst["segment"], "nsefo")
            sub_str = f"{seg}/{inst['instrument_id']}"
            if sub_str not in subs:
                subs.append(sub_str)

        self._subscriptions = subs
        log.info("subscription_list_built", count=len(subs))
        return subs

    async def close(self):
        await self._http.aclose()
