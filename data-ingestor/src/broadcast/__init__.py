"""In-memory broadcast publisher for real-time SSE consumers."""
import asyncio
import structlog
import orjson
from typing import AsyncIterator
from ..feed import MarketTick

log = structlog.get_logger()


class TickBroadcaster:
    """Broadcasts ticks to multiple SSE consumers via asyncio.Queue."""

    def __init__(self, maxsize: int = 1000):
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._maxsize = maxsize
        self._latest_ticks: dict[int, dict] = {}  # instrument_id -> latest tick

    async def publish(self, tick: MarketTick):
        """Publish a tick to all subscribers and update latest cache."""
        tick_dict = {
            "instrument_id": tick.instrument_id,
            "ltp": float(tick.ltp),
            "ltq": tick.ltq,
            "volume": tick.total_traded_volume,
            "open": float(tick.open_price),
            "high": float(tick.high_price),
            "low": float(tick.low_price),
            "close": float(tick.close_price),
            "bid": float(tick.best_bid_price),
            "ask": float(tick.best_ask_price),
            "timestamp": tick.timestamp.isoformat(),
        }

        # Update latest cache
        self._latest_ticks[tick.instrument_id] = tick_dict

        # Broadcast to all subscribers
        msg = orjson.dumps(tick_dict).decode()
        dead_subs = []

        for sub_id, queue in self._subscribers.items():
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Drop oldest message for slow consumers
                try:
                    queue.get_nowait()
                    queue.put_nowait(msg)
                except asyncio.QueueEmpty:
                    pass
            except Exception:
                dead_subs.append(sub_id)

        for sub_id in dead_subs:
            self._subscribers.pop(sub_id, None)

    def subscribe(self, subscriber_id: str) -> asyncio.Queue:
        """Register a new subscriber and return their queue."""
        queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers[subscriber_id] = queue
        log.info("subscriber_added", id=subscriber_id, total=len(self._subscribers))
        return queue

    def unsubscribe(self, subscriber_id: str):
        """Remove a subscriber."""
        self._subscribers.pop(subscriber_id, None)
        log.info("subscriber_removed", id=subscriber_id, total=len(self._subscribers))

    def get_latest(self, instrument_id: int = None) -> dict:
        """Get latest tick(s) from cache.

        Args:
            instrument_id: Specific instrument or None for all.
        """
        if instrument_id:
            return self._latest_ticks.get(instrument_id, {})
        return dict(self._latest_ticks)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Global broadcaster instance (shared between ingestor and API)
broadcaster = TickBroadcaster()
