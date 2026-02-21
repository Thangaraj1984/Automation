"""Compare live MQTT ticks with Market Data API for a single option strike.

Usage examples:
  - DB/API only (no MQTT credentials):
      python scripts/compare_mqtt_api.py --symbol NIFTY --strike 25700 --opt CE --expiry 2026-02-24 --duration 10 --api http://localhost:8088

  - With IIFL session token (live MQTT):
      export IIFL_SESSION_TOKEN="<jwt>"
      python scripts/compare_mqtt_api.py --symbol NIFTY --strike 25700 --opt CE --expiry 2026-02-24 --duration 15 --api http://localhost:8088 --mqtt-host bridge.iiflcapital.com

What it does:
  1. Fetches option-chain from API and finds instrument_id for the given strike.
  2. (Optional) Subscribes to MQTT topic for that instrument and captures live LTPs.
  3. Polls the API for the same strike repeatedly during the window.
  4. Prints a short comparison and exits.

Notes:
  - If you don't provide an MQTT session token via IIFL_SESSION_TOKEN env var, the script will perform API/DB-only comparisons.
  - Requires `paho-mqtt` and `requests`.
"""
import argparse
import os
import time
import threading
import struct
import requests

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None


def find_instrument_from_chain(chain_json, strike, opt_type):
    for row in chain_json.get("data", {}).get("chain", []):
        s = float(row.get("strike"))
        if s == float(strike):
            side = row.get("ce") if opt_type == "CE" else row.get("pe")
            if side:
                return side.get("instrument_id"), side.get("ltp")
    return None, None


class MQTTCollector:
    def __init__(self, host, port, token, instrument_token):
        self.host = host
        self.port = port
        self.token = token
        self.instrument_token = instrument_token  # numeric token e.g. 35005
        self.samples = []
        self.client = None
        self._stop = threading.Event()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0 or (hasattr(rc, 'value') and rc.value == 0):
            topic = f"prod/marketfeed/mw/v1/nsefo/{self.instrument_token}"
            client.subscribe([(topic, 0)])
            print(f"Subscribed MQTT -> {topic}")
        else:
            print("MQTT connect failed rc=", rc)

    def _on_message(self, client, userdata, message):
        payload = message.payload
        # Parse per official IIFL docs: bytes 0-3 = LTP, bytes 58-61 = priceDivisor
        if len(payload) >= 66:
            ltp_raw = struct.unpack_from('<i', payload, 0)[0]
            price_divisor = struct.unpack_from('<i', payload, 58)[0]
            divisor = float(price_divisor) if price_divisor > 0 else 100.0
            ltp = ltp_raw / divisor
            ts = time.time()
            self.samples.append((ts, ltp))

    def start(self):
        if mqtt is None:
            raise RuntimeError("paho-mqtt not installed")
        username = None
        password = None
        # token expected as JWT in env IIFL_SESSION_TOKEN
        if self.token:
            parts = self.token.split('.')
            username = 'mqttuser'
            password = f"OPENID~~{self.token}~"

        self.client = mqtt.Client(protocol=mqtt.MQTTv311, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.client.tls_set(cert_reqs=None)
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(self.host, self.port, keepalive=20)
        self.client.loop_start()

    def stop(self):
        try:
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description="Compare MQTT live ticks with Market Data API for a strike")
    p.add_argument("--symbol", default="NIFTY")
    p.add_argument("--strike", required=True, type=str, help="Strike price (number) or 'ATM'")
    p.add_argument("--opt", choices=["CE","PE"], default="CE")
    p.add_argument("--expiry", required=False)
    p.add_argument("--duration", type=int, default=10, help="seconds to record/poll")
    p.add_argument("--api", default="http://localhost:8088")
    p.add_argument("--api-key", default=None, help="X-API-Key for protected API endpoints")
    p.add_argument("--mqtt-host", default="bridge.iiflcapital.com")
    p.add_argument("--mqtt-port", type=int, default=8883)
    args = p.parse_args()

    api_base = args.api.rstrip('/')
    api_headers = {"X-API-Key": args.api_key} if args.api_key else {}

    # 1) Fetch chain and locate instrument_id
    params = {}
    if args.expiry:
        params['expiry'] = args.expiry

    url = f"{api_base}/api/v1/chain/{args.symbol}"
    print(f"Querying API: {url} {params}")
    r = requests.get(url, params=params, headers=api_headers)
    if r.status_code != 200:
        print("API error:", r.status_code, r.text)
        return

    chain = r.json()

    # Support 'ATM' keyword: pick nearest strike to underlying_ltp
    strike_input = args.strike.strip()
    if strike_input.lower() == 'atm':
        underlying = chain.get('data', {}).get('underlying_ltp')
        if not underlying:
            print('Unable to detect ATM (underlying_ltp not present in API response)')
            return
        # find nearest strike in chain
        strikes = [float(r.get('strike')) for r in chain.get('data', {}).get('chain', [])]
        if not strikes:
            print('No strikes found in API chain')
            return
        strike_val = min(strikes, key=lambda s: abs(s - float(underlying)))
        print(f"ATM detected → underlying_ltp={underlying} → nearest strike={strike_val}")
    else:
        try:
            strike_val = float(strike_input)
        except ValueError:
            print('Invalid strike value')
            return

    inst_id, api_ltp = find_instrument_from_chain(chain, strike_val, args.opt)
    if not inst_id:
        print("Instrument not found in API chain for given strike/option_type")
        return

    print(f"Found instrument_id={inst_id} (API LTP={api_ltp})")
    # replace args.strike with resolved numeric strike for later reporting
    args.strike = strike_val

    # 2) Start MQTT collector if token provided
    token = os.getenv('IIFL_SESSION_TOKEN')
    collector = None
    if token:
        if mqtt is None:
            print("paho-mqtt not installed — skipping MQTT capture")
            token = None
        else:
            collector = MQTTCollector(args.mqtt_host, args.mqtt_port, token, inst_id)
            collector.start()
            print("Started MQTT collector (live)")
    else:
        print("No IIFL_SESSION_TOKEN found — MQTT capture skipped")

    # 3) Poll API ticks for the same strike while optionally collecting MQTT samples
    end_t = time.time() + args.duration
    api_samples = []
    while time.time() < end_t:
        r = requests.get(url, params=params, headers=api_headers)
        if r.status_code == 200:
            j = r.json()
            inst_id_api, ltp_api = find_instrument_from_chain(j, args.strike, args.opt)
            api_samples.append((time.time(), ltp_api))
        else:
            print(f"API poll error {r.status_code}: {r.text}")
        time.sleep(1)

    # stop MQTT collector
    if collector:
        collector.stop()

    # 4) Print results
    print('\n=== Results ===')
    print(f"Instrument: {args.symbol} strike={args.strike} {args.opt} -> instrument_id={inst_id}")

    print('\nAPI samples:')
    for ts, l in api_samples:
        print(f"  {time.strftime('%H:%M:%S', time.localtime(ts))}  LTP={l}")

    if collector and collector.samples:
        print('\nMQTT samples:')
        for ts, l in collector.samples:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(ts))}  LTP={l}")

    # Quick comparison summary
    api_vals = [v for _, v in api_samples if v is not None]
    mqtt_vals = [v for _, v in (collector.samples if collector else [])]

    if api_vals and mqtt_vals:
        avg_api = sum(api_vals)/len(api_vals)
        avg_mqtt = sum(mqtt_vals)/len(mqtt_vals)
        diff = avg_mqtt - avg_api
        print('\nSummary:')
        print(f"  API avg LTP = {avg_api:.2f}  |  MQTT avg LTP = {avg_mqtt:.2f}  |  diff = {diff:.2f}")
        if abs(diff) > max(1.0, 0.01 * avg_api):
            print("  ⚠️  Significant mismatch detected — possible ingest/mapping error or stale API data")
        else:
            print("  ✅  API and MQTT LTPs are consistent")
    elif api_vals and not mqtt_vals:
        print('\nOnly API data available (no MQTT samples). Use IIFL_SESSION_TOKEN to enable live MQTT capture.')
    elif mqtt_vals and not api_vals:
        print('\nOnly MQTT samples available — API did not return LTP for this strike')
    else:
        print('\nNo samples captured — check connectivity and API availability')

    # Helpful next steps
    print('\nNext steps:')
    print(f"  - Query API tick history: {api_base}/api/v1/ticks/{inst_id}")
    print("  - If mismatch: check ingest logs (data-ingestor) and DB rows for neighboring instrument_ids")


if __name__ == '__main__':
    main()
