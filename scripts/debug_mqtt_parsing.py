"""Debug script: capture raw MQTT binary for instrument 64860 and parse
both with the CURRENT parser logic and the OFFICIAL IIFL doc layout.

Usage:
  $env:IIFL_SESSION_TOKEN = '<jwt>'
  python scripts/debug_mqtt_parsing.py --instrument 64860 --count 5
"""
import argparse
import os
import struct
import time
import threading

try:
    import paho.mqtt.client as mqtt
except ImportError:
    raise SystemExit("paho-mqtt required: pip install paho-mqtt")


class RawCapture:
    def __init__(self, host, port, token, instrument_id, max_count=5):
        self.host = host
        self.port = port
        self.token = token
        self.instrument_id = instrument_id
        self.max_count = max_count
        self.packets = []
        self._done = threading.Event()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        rc_val = rc.value if hasattr(rc, 'value') else rc
        if rc_val == 0:
            topic = f"prod/marketfeed/mw/v1/nsefo/{self.instrument_id}"
            client.subscribe([(topic, 0)])
            print(f"Subscribed to {topic}")
        else:
            print(f"Connect failed rc={rc_val}")

    def _on_message(self, client, userdata, message):
        payload = message.payload
        self.packets.append((time.time(), message.topic, bytes(payload)))
        if len(self.packets) >= self.max_count:
            self._done.set()

    def capture(self, timeout=30):
        client = mqtt.Client(protocol=mqtt.MQTTv311,
                             callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        client.tls_set(cert_reqs=None)
        client.username_pw_set("mqttuser", f"OPENID~~{self.token}~")
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect(self.host, self.port, keepalive=20)
        client.loop_start()
        self._done.wait(timeout=timeout)
        client.loop_stop()
        client.disconnect()
        return self.packets


def parse_current(data: bytes):
    """Current ingester parsing (header[1] = ltp)."""
    if len(data) < 48:
        return None
    header = struct.unpack_from('<12i', data, 0)
    divisor = 100.0
    return {
        'payload_token': header[0],
        'ltp': header[1] / divisor,
        'ltq': header[2],
        'total_volume': header[3],
        'open': header[4] / divisor,
        'high': header[5] / divisor,
        'low': header[6] / divisor,
        'close': header[7] / divisor,
        'best_bid_price': header[8] / divisor,
        'best_bid_qty': header[9],
        'best_ask_price': header[10] / divisor,
        'best_ask_qty': header[11],
    }


def parse_official_docs(data: bytes):
    """Parse according to official IIFL docs (188 bytes).
    
    Official layout:
      0-3:   ltp (Int32)
      4-7:   lastTradedQuantity (UInt32)
      8-11:  tradedVolume (UInt32)
      12-15: high (Int32)
      16-19: low (Int32)
      20-23: open (Int32)
      24-27: close (Int32)
      28-31: averageTradedPrice (Int32)
      32-33: reserved (UInt16)
      34-37: bestBidQuantity (UInt32)
      38-41: bestBidPrice (Int32)
      42-45: bestAskQuantity (UInt32)
      46-49: bestAskPrice (Int32)
      50-53: totalBidQuantity (UInt32)
      54-57: totalAskQuantity (UInt32)
      58-61: priceDivisor (Int32)
      62-65: lastTradedTime (Int32)
    """
    if len(data) < 66:
        return None

    fmt = '<i I I i i i i i H I i I i I I i i'
    #      ltp ltq vol high low open close avgTrdPrc reserved bbQty bbPrc baQty baPrc tbQty taQty priceDivisor lastTrdTime
    vals = struct.unpack_from(fmt, data, 0)
    
    ltp_raw = vals[0]
    ltq = vals[1]
    volume = vals[2]
    high_raw = vals[3]
    low_raw = vals[4]
    open_raw = vals[5]
    close_raw = vals[6]
    avg_price_raw = vals[7]
    reserved = vals[8]
    bb_qty = vals[9]
    bb_price_raw = vals[10]
    ba_qty = vals[11]
    ba_price_raw = vals[12]
    total_bid_qty = vals[13]
    total_ask_qty = vals[14]
    price_divisor = vals[15]
    last_traded_time = vals[16]

    divisor = float(price_divisor) if price_divisor > 0 else 100.0

    return {
        'ltp': ltp_raw / divisor,
        'lastTradedQuantity': ltq,
        'tradedVolume': volume,
        'high': high_raw / divisor,
        'low': low_raw / divisor,
        'open': open_raw / divisor,
        'close': close_raw / divisor,
        'averageTradedPrice': avg_price_raw / divisor,
        'reserved': reserved,
        'bestBidQty': bb_qty,
        'bestBidPrice': bb_price_raw / divisor,
        'bestAskQty': ba_qty,
        'bestAskPrice': ba_price_raw / divisor,
        'totalBidQty': total_bid_qty,
        'totalAskQty': total_ask_qty,
        'priceDivisor': price_divisor,
        'lastTradedTime': last_traded_time,
        # raw values for inspection
        '_raw_ltp': ltp_raw,
        '_raw_ltq': ltq,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--instrument', type=int, default=64860)
    p.add_argument('--count', type=int, default=5)
    p.add_argument('--host', default='bridge.iiflcapital.com')
    p.add_argument('--port', type=int, default=8883)
    args = p.parse_args()

    token = os.getenv('IIFL_SESSION_TOKEN')
    if not token:
        print('Set IIFL_SESSION_TOKEN env var first')
        return

    print(f"Capturing {args.count} MQTT packets for instrument {args.instrument}...")
    cap = RawCapture(args.host, args.port, token, args.instrument, args.count)
    packets = cap.capture(timeout=60)

    if not packets:
        print("No packets captured!")
        return

    for i, (ts, topic, data) in enumerate(packets):
        print(f"\n{'='*80}")
        print(f"Packet {i+1} | Topic: {topic} | Size: {len(data)} bytes")
        print(f"Hex (first 66 bytes): {data[:66].hex()}")

        current = parse_current(data)
        official = parse_official_docs(data)

        print(f"\n--- CURRENT parser (header[1]/100 = ltp) ---")
        if current:
            print(f"  payload_token = {current['payload_token']}")
            print(f"  ltp           = {current['ltp']}")
            print(f"  ltq           = {current['ltq']}")
            print(f"  total_volume  = {current['total_volume']}")
            print(f"  open          = {current['open']}")
            print(f"  high          = {current['high']}")
            print(f"  low           = {current['low']}")
            print(f"  close         = {current['close']}")
            print(f"  best_bid_price= {current['best_bid_price']}")
            print(f"  best_bid_qty  = {current['best_bid_qty']}")
            print(f"  best_ask_price= {current['best_ask_price']}")
            print(f"  best_ask_qty  = {current['best_ask_qty']}")

        print(f"\n--- OFFICIAL docs parser (byte 0-3 = ltp, byte 58-61 = divisor) ---")
        if official:
            print(f"  ltp              = {official['ltp']}  (raw={official['_raw_ltp']})")
            print(f"  lastTradedQty    = {official['lastTradedQuantity']}")
            print(f"  tradedVolume     = {official['tradedVolume']}")
            print(f"  high             = {official['high']}")
            print(f"  low              = {official['low']}")
            print(f"  open             = {official['open']}")
            print(f"  close            = {official['close']}")
            print(f"  avgTradedPrice   = {official['averageTradedPrice']}")
            print(f"  priceDivisor     = {official['priceDivisor']}")
            print(f"  bestBidQty       = {official['bestBidQty']}")
            print(f"  bestBidPrice     = {official['bestBidPrice']}")
            print(f"  bestAskQty       = {official['bestAskQty']}")
            print(f"  bestAskPrice     = {official['bestAskPrice']}")
            print(f"  totalBidQty      = {official['totalBidQty']}")
            print(f"  totalAskQty      = {official['totalAskQty']}")
            print(f"  lastTradedTime   = {official['lastTradedTime']}")

        print(f"\n--- COMPARISON ---")
        if current and official:
            print(f"  Current LTP:  {current['ltp']}")
            print(f"  Official LTP: {official['ltp']}")
            print(f"  Match: {'YES' if abs(current['ltp'] - official['ltp']) < 0.01 else 'NO'}")
            if abs(current['ltp'] - official['ltp']) > 0.01:
                print(f"  Current uses bytes 4-7 as LTP = {current['ltp']}")
                print(f"  Official uses bytes 0-3 as LTP = {official['ltp']}")
                print(f"  bytes 0-3 raw = {official['_raw_ltp']} (looks like {'counter/token' if official['_raw_ltp'] < 100000 and official['_raw_ltp'] > 0 else 'ltp'})")


if __name__ == '__main__':
    main()
