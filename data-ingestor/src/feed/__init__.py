"""IIFL Capital Markets MQTT binary packet parsers.

Parses binary market feed packets from IIFL MQTT:
- Market Watch (MW): ~186 bytes - price, volume, OHLC, bid/ask, depth
- Open Interest (OI): 16 bytes - OI, dayHighOI, dayLowOI, previousOI

Packet structures based on BridgePy reference implementation.
"""
import struct
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class MarketTick:
    """Parsed market data tick from IIFL binary feed."""
    instrument_id: int
    ltp: float
    ltq: int
    total_traded_volume: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    best_bid_price: float
    best_bid_qty: int
    best_ask_price: float
    best_ask_qty: int
    # Market depth (5 levels)
    bid_prices: list[float]
    bid_qtys: list[int]
    ask_prices: list[float]
    ask_qtys: list[int]
    timestamp: datetime
    # Original token from binary payload (useful for diagnostics). Not written to DB.
    payload_token: int | None = None

    def to_db_tuple(self) -> tuple:
        """Convert to tuple for batch DB insert."""
        return (
            self.timestamp,
            self.instrument_id,
            self.ltp,
            self.ltq,
            self.total_traded_volume,
            self.open_price,
            self.high_price,
            self.low_price,
            self.close_price,
            self.best_bid_price,
            self.best_bid_qty,
            self.best_ask_price,
            self.best_ask_qty,
            # Depth Level 1-5
            self.bid_prices[0] if len(self.bid_prices) > 0 else 0,
            self.bid_qtys[0] if len(self.bid_qtys) > 0 else 0,
            self.bid_prices[1] if len(self.bid_prices) > 1 else 0,
            self.bid_qtys[1] if len(self.bid_qtys) > 1 else 0,
            self.bid_prices[2] if len(self.bid_prices) > 2 else 0,
            self.bid_qtys[2] if len(self.bid_qtys) > 2 else 0,
            self.bid_prices[3] if len(self.bid_prices) > 3 else 0,
            self.bid_qtys[3] if len(self.bid_qtys) > 3 else 0,
            self.bid_prices[4] if len(self.bid_prices) > 4 else 0,
            self.bid_qtys[4] if len(self.bid_qtys) > 4 else 0,
            self.ask_prices[0] if len(self.ask_prices) > 0 else 0,
            self.ask_qtys[0] if len(self.ask_qtys) > 0 else 0,
            self.ask_prices[1] if len(self.ask_prices) > 1 else 0,
            self.ask_qtys[1] if len(self.ask_qtys) > 1 else 0,
            self.ask_prices[2] if len(self.ask_prices) > 2 else 0,
            self.ask_qtys[2] if len(self.ask_qtys) > 2 else 0,
            self.ask_prices[3] if len(self.ask_prices) > 3 else 0,
            self.ask_qtys[3] if len(self.ask_qtys) > 3 else 0,
            self.ask_prices[4] if len(self.ask_prices) > 4 else 0,
            self.ask_qtys[4] if len(self.ask_qtys) > 4 else 0,
        )


@dataclass
class OITick:
    """Parsed OI data tick from IIFL MQTT OI feed.

    Binary layout (16 bytes = 4 x int32 little-endian):
        openInterest:  Current open interest
        dayHighOi:     Day's high OI
        dayLowOi:      Day's low OI
        previousOi:    Previous day's closing OI
    """
    instrument_id: int
    open_interest: int
    day_high_oi: int
    day_low_oi: int
    previous_oi: int
    change_in_oi: int  # computed: open_interest - previous_oi
    timestamp: datetime

    def to_db_tuple(self) -> tuple:
        """Convert to tuple for DB insert."""
        return (
            self.timestamp,
            self.instrument_id,
            self.open_interest,
            self.change_in_oi,
            self.day_high_oi,
            self.day_low_oi,
            self.previous_oi,
        )


def parse_binary_packet(data: bytes) -> Optional[MarketTick]:
    """Parse IIFL Capital Markets 188-byte binary market feed packet.

    Official IIFL packet layout (https://developers.iiflcapital.com/apidocs/marketdatastream):
    Offset  Size  Type    Field
    0-3     4     Int32   ltp
    4-7     4     UInt32  lastTradedQuantity
    8-11    4     UInt32  tradedVolume
    12-15   4     Int32   high
    16-19   4     Int32   low
    20-23   4     Int32   open
    24-27   4     Int32   close
    28-31   4     Int32   averageTradedPrice
    32-33   2     UInt16  reserved
    34-37   4     UInt32  bestBidQuantity
    38-41   4     Int32   bestBidPrice
    42-45   4     UInt32  bestAskQuantity
    46-49   4     Int32   bestAskPrice
    50-53   4     UInt32  totalBidQuantity
    54-57   4     UInt32  totalAskQuantity
    58-61   4     Int32   priceDivisor
    62-65   4     Int32   lastTradedTime
    66-125  60    ...     marketDepth bids (5 levels x 12 bytes each)
    126-185 60    ...     marketDepth asks (5 levels x 12 bytes each)
    186-187 2     Int16   reserved (to be ignored)

    Each depth level: quantity(UInt32,4) + price(Int32,4) + orders(Int16,2) + ignored(Int16,2) = 12 bytes

    All prices must be divided by priceDivisor (bytes 58-61) to get actual values.
    Instrument ID comes from the MQTT topic, NOT from the binary payload.
    """
    if not data or len(data) < 66:
        return None

    try:
        # Parse fixed header per official docs:
        # '<i I I i i i i i H I i I i I I i i'
        #  ltp ltq vol high low open close avg reserved bbQty bbPrice baQty baPrice tbQty taQty divisor lastTrdTime
        header_fmt = '<i I I i i i i i H I i I i I I i i'
        vals = struct.unpack_from(header_fmt, data, 0)

        ltp_raw = vals[0]
        ltq = vals[1]
        total_volume = vals[2]
        high_raw = vals[3]
        low_raw = vals[4]
        open_raw = vals[5]
        close_raw = vals[6]
        # vals[7] = averageTradedPrice (not stored)
        # vals[8] = reserved (UInt16)
        best_bid_qty = vals[9]
        best_bid_price_raw = vals[10]
        best_ask_qty = vals[11]
        best_ask_price_raw = vals[12]
        # vals[13] = totalBidQuantity
        # vals[14] = totalAskQuantity
        price_divisor = vals[15]
        # vals[16] = lastTradedTime

        divisor = float(price_divisor) if price_divisor > 0 else 100.0

        ltp = ltp_raw / divisor
        open_price = open_raw / divisor
        high_price = high_raw / divisor
        low_price = low_raw / divisor
        close_price = close_raw / divisor
        best_bid_price = best_bid_price_raw / divisor
        best_ask_price = best_ask_price_raw / divisor

        # Parse market depth (5 bid levels at byte 66, 5 ask levels at byte 126)
        # Each level: quantity(UInt32,4) + price(Int32,4) + orders(Int16,2) + ignored(Int16,2) = 12 bytes
        bid_prices = []
        bid_qtys = []
        ask_prices = []
        ask_qtys = []

        if len(data) >= 186:
            for i in range(5):
                bid_offset = 66 + (i * 12)
                bid_qty_val, bid_price_val = struct.unpack_from('<Ii', data, bid_offset)
                bid_prices.append(bid_price_val / divisor)
                bid_qtys.append(bid_qty_val)

                ask_offset = 126 + (i * 12)
                ask_qty_val, ask_price_val = struct.unpack_from('<Ii', data, ask_offset)
                ask_prices.append(ask_price_val / divisor)
                ask_qtys.append(ask_qty_val)

        # Pad to 5 levels if needed
        while len(bid_prices) < 5:
            bid_prices.append(0.0)
            bid_qtys.append(0)
            ask_prices.append(0.0)
            ask_qtys.append(0)

        return MarketTick(
            instrument_id=0,  # Will be set from MQTT topic by ws_client
            ltp=ltp,
            ltq=ltq,
            total_traded_volume=total_volume,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=close_price,
            best_bid_price=best_bid_price,
            best_bid_qty=best_bid_qty,
            best_ask_price=best_ask_price,
            best_ask_qty=best_ask_qty,
            bid_prices=bid_prices,
            bid_qtys=bid_qtys,
            ask_prices=ask_prices,
            ask_qtys=ask_qtys,
            timestamp=datetime.now(IST),
            payload_token=ltp_raw,  # Store raw LTP for diagnostics
        )
    except struct.error:
        return None


def parse_oi_packet(data: bytes, instrument_id: int) -> Optional[OITick]:
    """Parse IIFL OI binary packet (16 bytes = 4 x int32 little-endian).

    From BridgePy examples/main.py:
        format = "iiii"
        unpacked = struct.unpack(format, data)
        openInterest, dayHighOi, dayLowOi, previousOi
    """
    if not data or len(data) < 16:
        return None

    try:
        oi, day_high, day_low, prev_oi = struct.unpack("<iiii", data[:16])
        return OITick(
            instrument_id=instrument_id,
            open_interest=oi,
            day_high_oi=day_high,
            day_low_oi=day_low,
            previous_oi=prev_oi,
            change_in_oi=oi - prev_oi,
            timestamp=datetime.now(IST),
        )
    except struct.error:
        return None


def parse_json_tick(data: dict) -> Optional[MarketTick]:
    """Fallback parser for JSON-formatted tick data from IIFL."""
    try:
        return MarketTick(
            instrument_id=int(data.get("instrumentId", data.get("token", 0))),
            ltp=float(data.get("ltp", 0)),
            ltq=int(data.get("ltq", data.get("lastTradedQty", 0))),
            total_traded_volume=int(data.get("ttv", data.get("totalTradedVolume", 0))),
            open_price=float(data.get("open", 0)),
            high_price=float(data.get("high", 0)),
            low_price=float(data.get("low", 0)),
            close_price=float(data.get("close", 0)),
            best_bid_price=float(data.get("bestBidPrice", data.get("bid", 0))),
            best_bid_qty=int(data.get("bestBidQty", 0)),
            best_ask_price=float(data.get("bestAskPrice", data.get("ask", 0))),
            best_ask_qty=int(data.get("bestAskQty", 0)),
            bid_prices=[0.0]*5,
            bid_qtys=[0]*5,
            ask_prices=[0.0]*5,
            ask_qtys=[0]*5,
            timestamp=datetime.now(IST),
            payload_token=None,
        )
    except (KeyError, ValueError, TypeError):
        return None
