"""Database connection pool and batch writer for TimescaleDB."""
import asyncio
import asyncpg
import structlog
from datetime import datetime, timezone, timedelta
from typing import Optional
from ..config import Config
from ..feed import MarketTick, OITick

log = structlog.get_logger()
IST = timezone(timedelta(hours=5, minutes=30))


class DatabaseManager:
    """Manages asyncpg connection pool and batch writes to TimescaleDB."""

    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self._tick_buffer: list[tuple] = []
        self._oi_buffer: list[tuple] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._total_ticks_written = 0
        self._total_oi_written = 0

    async def connect(self):
        """Create connection pool and ensure schema is up to date."""
        log.info("db_connecting", host=Config.DB_HOST, port=Config.DB_PORT, db=Config.DB_NAME)

        self.pool = await asyncpg.create_pool(
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            database=Config.DB_NAME,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )

        log.info("db_connected")

        # Ensure OI table has the new columns for MQTT feed data
        await self._migrate_oi_table()

        # Start periodic flush task
        self._flush_task = asyncio.create_task(self._periodic_flush())

    async def _migrate_oi_table(self):
        """Add new columns if they don't exist (for MQTT OI feed data)."""
        async with self.pool.acquire() as conn:
            # Check if new columns exist, add them if not
            for col, col_type in [
                ("day_high_oi", "BIGINT"),
                ("day_low_oi", "BIGINT"),
                ("previous_oi", "BIGINT"),
            ]:
                exists = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'oi_data' AND column_name = $1
                    )
                """, col)
                if not exists:
                    await conn.execute(
                        f"ALTER TABLE oi_data ADD COLUMN {col} {col_type}"
                    )
                    log.info("db_migration", action="added_column",
                             table="oi_data", column=col)

    async def buffer_tick(self, tick: MarketTick):
        """Add a tick to the write buffer. Flushes when threshold reached."""
        async with self._buffer_lock:
            self._tick_buffer.append(tick.to_db_tuple())

            if len(self._tick_buffer) >= Config.BATCH_SIZE:
                await self._flush_ticks()

    async def buffer_oi(self, oi_tick: OITick):
        """Add an OI tick to the write buffer."""
        async with self._buffer_lock:
            self._oi_buffer.append(oi_tick.to_db_tuple())

            if len(self._oi_buffer) >= Config.BATCH_SIZE:
                await self._flush_oi()

    async def _periodic_flush(self):
        """Periodically flush both buffers even if batch size not reached."""
        while True:
            await asyncio.sleep(Config.BATCH_FLUSH_INTERVAL)
            async with self._buffer_lock:
                if self._tick_buffer:
                    await self._flush_ticks()
                if self._oi_buffer:
                    await self._flush_oi()

    async def _flush_ticks(self):
        """Write buffered ticks to TimescaleDB using COPY for speed."""
        if not self._tick_buffer or not self.pool:
            return

        records = list(self._tick_buffer)
        self._tick_buffer.clear()

        try:
            # Validate instrument_ids in this batch — log (and optionally drop) any unknown ids.
            instrument_ids = list({r[1] for r in records})  # instrument_id is at index 1 of the tuple
            async with self.pool.acquire() as conn:
                if instrument_ids:
                    rows = await conn.fetch("SELECT instrument_id FROM instruments WHERE instrument_id = ANY($1::bigint[])", instrument_ids)
                    found_ids = {r['instrument_id'] for r in rows}
                    missing = set(instrument_ids) - found_ids
                    if missing:
                        log.warning("unknown_instrument_ids_in_tick_batch", missing_ids=list(missing), batch_count=len(records))
                        if Config.DROP_UNKNOWN_TICKS:
                            # Drop records with unknown instrument_ids
                            filtered = [rec for rec in records if rec[1] in found_ids]
                            if not filtered:
                                log.warning("tick_batch_all_dropped_due_to_unknown_ids", dropped_count=len(records))
                                return
                            records = filtered

                await conn.copy_records_to_table(
                    'tick_data',
                    records=records,
                    columns=[
                        'time', 'instrument_id', 'ltp', 'ltq', 'total_traded_volume',
                        'open_price', 'high_price', 'low_price', 'close_price',
                        'best_bid_price', 'best_bid_qty', 'best_ask_price', 'best_ask_qty',
                        'bid_price_1', 'bid_qty_1', 'bid_price_2', 'bid_qty_2',
                        'bid_price_3', 'bid_qty_3', 'bid_price_4', 'bid_qty_4',
                        'bid_price_5', 'bid_qty_5',
                        'ask_price_1', 'ask_qty_1', 'ask_price_2', 'ask_qty_2',
                        'ask_price_3', 'ask_qty_3', 'ask_price_4', 'ask_qty_4',
                        'ask_price_5', 'ask_qty_5',
                    ],
                )
                self._total_ticks_written += len(records)

                if self._total_ticks_written % 5000 == 0:
                    log.info("db_tick_milestone",
                             total_written=self._total_ticks_written,
                             batch_size=len(records))

        except Exception as e:
            log.error("db_tick_write_error", error=str(e), lost_records=len(records))
            # Re-add to buffer on failure (with limit to avoid memory issues)
            if len(self._tick_buffer) < Config.BATCH_SIZE * 10:
                self._tick_buffer.extend(records)

    async def _flush_oi(self):
        """Write buffered OI records to TimescaleDB."""
        if not self._oi_buffer or not self.pool:
            return

        records = list(self._oi_buffer)
        self._oi_buffer.clear()

        try:
            async with self.pool.acquire() as conn:
                await conn.copy_records_to_table(
                    'oi_data',
                    records=records,
                    columns=[
                        'time', 'instrument_id', 'open_interest', 'change_in_oi',
                        'day_high_oi', 'day_low_oi', 'previous_oi',
                    ],
                )
                self._total_oi_written += len(records)

                if self._total_oi_written % 5000 == 0:
                    log.info("db_oi_milestone",
                             total_written=self._total_oi_written,
                             batch_size=len(records))

        except Exception as e:
            log.error("db_oi_write_error", error=str(e), lost_records=len(records))
            if len(self._oi_buffer) < Config.BATCH_SIZE * 10:
                self._oi_buffer.extend(records)

    async def write_historical(self, records: list[tuple]):
        """Batch write historical OHLCV data."""
        if not self.pool or not records:
            return

        async with self.pool.acquire() as conn:
            await conn.copy_records_to_table(
                'historical_ohlcv',
                records=records,
                columns=['time', 'instrument_id', 'interval_type',
                         'open_price', 'high_price', 'low_price', 'close_price',
                         'volume', 'oi'],
            )
            log.info("historical_written", count=len(records))

    async def get_latest_tick(self, instrument_id: int) -> Optional[dict]:
        """Get the most recent tick for an instrument."""
        if not self.pool:
            return None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT ltp, open_price, high_price, low_price, close_price,
                       total_traded_volume, time
                FROM tick_data
                WHERE instrument_id = $1
                ORDER BY time DESC LIMIT 1
            """, instrument_id)
            return dict(row) if row else None

    async def flush_and_close(self):
        """Flush remaining buffers and close pool."""
        if self._flush_task:
            self._flush_task.cancel()

        async with self._buffer_lock:
            if self._tick_buffer:
                await self._flush_ticks()
            if self._oi_buffer:
                await self._flush_oi()

        if self.pool:
            await self.pool.close()

        log.info("db_closed",
                 total_ticks=self._total_ticks_written,
                 total_oi=self._total_oi_written)
