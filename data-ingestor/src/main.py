"""NSE Real-Time Market Data Ingestor - Main Entry Point.

Orchestrates:
1. IIFL Capital Markets API authentication
2. Instrument master download and filtering
3. MQTT connection for real-time feed (price ticks + OI on same connection)
4. Batch writes to TimescaleDB
5. Historical data backfill (optional)

All real-time data (price ticks AND open interest) comes from a single
MQTT connection to bridge.iiflcapital.com:8883 — no REST API polling needed.
"""
import asyncio
import signal
import sys
import structlog
from datetime import datetime, timezone, timedelta

from .config import Config
from .utils import setup_logging
from .auth import SessionManager
from .instruments import InstrumentMaster, NIFTY_INDEX_INSTRUMENT_ID
from .feed.ws_client import MQTTFeedClient
from .feed import MarketTick, OITick
from .db import DatabaseManager
from .broadcast import broadcaster
from .backfill import HistoricalBackfill

IST = timezone(timedelta(hours=5, minutes=30))
log = None


class DataIngestor:
    """Main orchestrator for the NSE data ingestor service."""

    def __init__(self):
        self.session = SessionManager()
        self.db = DatabaseManager()
        self.instruments_mgr: InstrumentMaster | None = None
        self.ws_client: MQTTFeedClient | None = None
        self._active_instruments: list[dict] = []
        self._shutdown_event = asyncio.Event()

    async def on_tick(self, tick: MarketTick):
        """Callback for each received market tick (MW feed).

        Special handling for NIFTY 50 index (instrument_id=999920000):
        The MW binary packet for indices has ltp=0, but the actual
        index value is in the total_traded_volume field (divided by 100).
        """
        # For index instruments, the "volume" field contains the index value
        if tick.instrument_id == NIFTY_INDEX_INSTRUMENT_ID and tick.ltp == 0:
            if tick.total_traded_volume > 0:
                tick.ltp = tick.total_traded_volume / 100.0
                tick.total_traded_volume = 0  # Reset since it's not actual volume

        # Optional sanity-check: drop/flag ticks with implausible LTP jumps
        if Config.LTP_SANITY_ENABLED and tick.ltp and tick.ltp > 0:
            try:
                latest = await self.db.get_latest_tick(tick.instrument_id)
                prev_ltp = float(latest.get('ltp')) if latest and latest.get('ltp') else None
                if prev_ltp and prev_ltp > 0:
                    pct = abs(tick.ltp - prev_ltp) / prev_ltp * 100.0
                    if pct > Config.LTP_SANITY_THRESHOLD_PCT:
                        log.warning("ltp_sanity_reject",
                                    instrument_id=tick.instrument_id,
                                    prev_ltp=prev_ltp,
                                    new_ltp=tick.ltp,
                                    pct=pct)
                        return  # drop suspicious tick
            except Exception as e:
                log.error("ltp_sanity_check_error", error=str(e))

        # Write to DB buffer
        await self.db.buffer_tick(tick)
        # Broadcast to SSE consumers
        await broadcaster.publish(tick)

    async def on_oi(self, oi_tick: OITick):
        """Callback for each received OI update (OI feed).

        OI data arrives via MQTT just like price ticks — no REST polling needed.
        Each update is stored as a new row (append-only, never overwritten).
        """
        await self.db.buffer_oi(oi_tick)

    async def start(self):
        """Initialize and start all components."""
        global log
        log = setup_logging()
        log.info("ingestor_starting", symbol=Config.SUBSCRIPTION_SYMBOL)

        # Validate config
        Config.validate()

        # Connect to database
        await self.db.connect()

        # Authenticate with IIFL Capital Markets API
        log.info("authenticating_iifl")
        session_token = await self.session.authenticate(db_pool=self.db.pool)
        log.info("authenticated_iifl", token_preview=session_token[:20] + "..." if session_token else "None")

        # Initialize instrument master
        self.instruments_mgr = InstrumentMaster(self.db.pool)

        # Download and filter instruments
        await self._refresh_instruments()

        if not self._active_instruments:
            log.error("no_instruments_found", symbol=Config.SUBSCRIPTION_SYMBOL)
            log.info("will_retry_in_60s")
            await asyncio.sleep(60)
            await self._refresh_instruments()

        if not self._active_instruments:
            raise RuntimeError(f"No instruments found for {Config.SUBSCRIPTION_SYMBOL}")

        # Build subscription list
        subscriptions = self.instruments_mgr.build_subscription_list(self._active_instruments)

        # Optional: Historical backfill
        if Config.ENABLE_BACKFILL:
            log.info("starting_backfill", days=Config.BACKFILL_DAYS)
            backfill = HistoricalBackfill(self.session, self.db)
            await backfill.backfill_all(self._active_instruments)

        # Create MQTT client with both tick and OI callbacks
        self.ws_client = MQTTFeedClient(
            session_token=session_token,
            on_tick=self.on_tick,
            on_oi=self.on_oi,
        )
        self.ws_client._subscriptions = subscriptions

        # Start instrument refresh task (every 5 minutes)
        asyncio.create_task(self._periodic_instrument_refresh())

        # Start JWT watcher (checks DB every 30s for new JWT from /auth page)
        asyncio.create_task(self._watch_jwt_updates())

        # Run MQTT client (blocking) — handles both MW and OI on same connection
        log.info("starting_mqtt_feed",
                 instruments=len(subscriptions),
                 topics_subscribed="MW + OI per instrument")
        await self.ws_client.run()

    async def _refresh_instruments(self):
        """Download instrument master and filter for target options."""
        instruments = await self.instruments_mgr.download_master(Config.SUBSCRIPTION_SEGMENT)

        # Get underlying LTP for ATM calculation from NIFTY 50 index
        underlying_ltp = 0
        if self.ws_client and self.ws_client.is_connected:
            latest = await self.db.get_latest_tick(NIFTY_INDEX_INSTRUMENT_ID)
            if latest:
                underlying_ltp = float(latest.get("ltp", 0))

        self._active_instruments = self.instruments_mgr.filter_weekly_options(
            symbol=Config.SUBSCRIPTION_SYMBOL,
            underlying_ltp=underlying_ltp,
            strike_range=Config.STRIKE_RANGE,
        )

        # Download and add NIFTY 50 index instrument for spot price tracking
        nifty_index = await self.instruments_mgr.download_index_master()
        if nifty_index:
            # Add to active instruments list for subscription
            self._active_instruments.append(nifty_index)
            log.info("nifty_index_added", instrument_id=nifty_index["instrument_id"])

        # Upsert to database
        await self.instruments_mgr.upsert_instruments(self._active_instruments)

        # Deactivate expired instruments (prevents cross-expiry data mixing)
        await self.instruments_mgr.deactivate_expired()

    async def _periodic_instrument_refresh(self):
        """Refresh instrument list every 5 minutes to adjust for ATM drift."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(300)  # 5 minutes
            try:
                old_count = len(self._active_instruments)
                await self._refresh_instruments()
                new_subs = self.instruments_mgr.build_subscription_list(self._active_instruments)

                if self.ws_client and self.ws_client.is_connected:
                    current_subs = set(self.ws_client._subscriptions)
                    new_subs_set = set(new_subs)

                    to_add = list(new_subs_set - current_subs)
                    to_remove = list(current_subs - new_subs_set)

                    if to_add:
                        await self.ws_client.subscribe(to_add)
                    if to_remove:
                        await self.ws_client.unsubscribe(to_remove)

                    self.ws_client._subscriptions = new_subs
                    log.info("instruments_refreshed", old=old_count,
                             new=len(self._active_instruments),
                             added=len(to_add), removed=len(to_remove))
            except Exception as e:
                log.error("instrument_refresh_error", error=str(e))

    async def _watch_jwt_updates(self):
        """Watch for new JWT tokens stored via the /auth web page.

        Checks the app_settings table every 30 seconds. If a newer JWT
        is found, updates the MQTT client's session token and reconnects.
        """
        last_jwt_time = None
        while not self._shutdown_event.is_set():
            await asyncio.sleep(30)
            try:
                async with self.db.pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT value, updated_at FROM app_settings WHERE key = 'iifl_session_token'"
                    )
                if not row or not row['value']:
                    continue

                # Check if this is a new JWT (newer than what we've seen)
                jwt_time = row['updated_at']
                if last_jwt_time is not None and jwt_time <= last_jwt_time:
                    continue  # Same JWT, skip

                last_jwt_time = jwt_time
                new_token = row['value']

                # Only reconnect if the token is actually different
                if self.ws_client and new_token != self.ws_client._session_token:
                    log.info("jwt_update_detected",
                             updated_at=str(jwt_time),
                             token_preview=new_token[:20] + "...")
                    self.session.session_token = new_token
                    self.ws_client._session_token = new_token

                    # Reconnect MQTT with new token
                    if self.ws_client.is_connected:
                        log.info("mqtt_reconnecting_with_new_jwt")
                        await self.ws_client.stop()
                        await asyncio.sleep(2)
                        await self.ws_client.run()

            except Exception as e:
                # Table might not exist yet — that's fine
                if "app_settings" not in str(e):
                    log.debug("jwt_watch_error", error=str(e))

    async def shutdown(self):
        """Graceful shutdown."""
        log.info("ingestor_shutting_down")
        self._shutdown_event.set()

        if self.ws_client:
            await self.ws_client.stop()

        await self.db.flush_and_close()
        await self.session.close()

        if self.instruments_mgr:
            await self.instruments_mgr.close()

        log.info("ingestor_shutdown_complete")


async def main():
    """Entry point."""
    ingestor = DataIngestor()

    # Handle signals
    loop = asyncio.get_event_loop()

    def signal_handler():
        asyncio.create_task(ingestor.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await ingestor.start()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error("ingestor_fatal_error", error=str(e), type=type(e).__name__)
    finally:
        await ingestor.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
