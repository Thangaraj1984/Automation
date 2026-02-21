"""Historical OHLCV data backfill from IIFL Capital Markets REST API.

Uses POST /marketdata/historicaldata with rate limiting (2 req/s).
"""
import asyncio
import structlog
from datetime import datetime, date, timedelta, timezone
from ..auth import SessionManager
from ..db import DatabaseManager
from ..config import Config

log = structlog.get_logger()
IST = timezone(timedelta(hours=5, minutes=30))


class HistoricalBackfill:
    """Backfills historical OHLCV data from IIFL REST API."""

    def __init__(self, session: SessionManager, db: DatabaseManager):
        self.session = session
        self.db = db

    async def backfill_instrument(
        self,
        instrument_id: int,
        exchange: str,
        days: int = 30,
        interval: str = "1m",
    ):
        """Backfill historical data for a single instrument.

        Args:
            instrument_id: IIFL instrument token
            exchange: Exchange segment (NSEFO, NSEEQ)
            days: Number of days to backfill
            interval: Candle interval (1m, 5m, 15m, 30m, 60m, 1d)
        """
        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        log.info("backfill_start", instrument_id=instrument_id,
                 start=str(start_date), end=str(end_date), interval=interval)

        # Process in chunks of 5 days to avoid large responses
        current = start_date
        total_records = 0

        while current < end_date:
            chunk_end = min(current + timedelta(days=5), end_date)

            try:
                payload = {
                    "instrumentId": instrument_id,
                    "exchangeSegment": exchange,
                    "fromDate": current.strftime("%Y-%m-%d"),
                    "toDate": chunk_end.strftime("%Y-%m-%d"),
                    "interval": interval,
                }

                resp = await self.session.market_request(
                    "/marketdata/historicaldata", payload
                )

                candles = self._extract_candles(resp)

                if candles:
                    records = []
                    for c in candles:
                        records.append((
                            c["time"],
                            instrument_id,
                            interval,
                            c["open"],
                            c["high"],
                            c["low"],
                            c["close"],
                            c.get("volume", 0),
                            c.get("oi", 0),
                        ))

                    await self.db.write_historical(records)
                    total_records += len(records)

                # Rate limit: 2 req/s
                await asyncio.sleep(0.5)

            except Exception as e:
                log.error("backfill_chunk_error", instrument_id=instrument_id,
                          start=str(current), end=str(chunk_end), error=str(e))

            current = chunk_end

        log.info("backfill_complete", instrument_id=instrument_id,
                 total_records=total_records)

    def _extract_candles(self, resp: dict) -> list[dict]:
        """Extract candle data from IIFL response."""
        candles = []

        # IIFL may return data in various formats
        data = resp.get("data", resp.get("candles", []))
        if isinstance(data, dict):
            data = data.get("candles", data.get("dataList", []))

        for item in data:
            if isinstance(item, list):
                # Array format: [timestamp, open, high, low, close, volume, oi]
                candles.append({
                    "time": self._parse_timestamp(item[0]),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": int(item[5]) if len(item) > 5 else 0,
                    "oi": int(item[6]) if len(item) > 6 else 0,
                })
            elif isinstance(item, dict):
                candles.append({
                    "time": self._parse_timestamp(
                        item.get("timestamp", item.get("time", item.get("dateTime")))
                    ),
                    "open": float(item.get("open", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "close": float(item.get("close", 0)),
                    "volume": int(item.get("volume", item.get("qty", 0))),
                    "oi": int(item.get("oi", item.get("openInterest", 0))),
                })

        return candles

    def _parse_timestamp(self, ts) -> datetime:
        """Parse timestamp from various formats."""
        if isinstance(ts, (int, float)):
            # Unix timestamp
            return datetime.fromtimestamp(ts, tz=IST)
        if isinstance(ts, str):
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(ts.split("+")[0].split(".")[0], fmt)
                    return dt.replace(tzinfo=IST)
                except ValueError:
                    continue
        return datetime.now(IST)

    async def backfill_all(self, instruments: list[dict]):
        """Backfill historical data for all instruments.

        Args:
            instruments: List of instrument dicts with instrument_id and segment.
        """
        log.info("backfill_all_start", count=len(instruments),
                 days=Config.BACKFILL_DAYS)

        for i, inst in enumerate(instruments):
            log.info("backfill_progress", current=i+1, total=len(instruments),
                     instrument_id=inst["instrument_id"])

            exchange_map = {"FO": "NSEFO", "EQ": "NSEEQ"}
            exchange = exchange_map.get(inst.get("segment", "FO"), "NSEFO")

            await self.backfill_instrument(
                instrument_id=inst["instrument_id"],
                exchange=exchange,
                days=Config.BACKFILL_DAYS,
                interval="1m",
            )

            # Additional 1-day candles for longer history
            await self.backfill_instrument(
                instrument_id=inst["instrument_id"],
                exchange=exchange,
                days=min(Config.BACKFILL_DAYS * 3, 365),
                interval="1d",
            )

        log.info("backfill_all_complete")
