"""Compare live MQTT feed with IIFL Market Data API for a given instrument_id.

Usage:
  # uses IIFL_SESSION_TOKEN from environment (.env)
  python scripts/compare_iifl_mqtt_instrument.py --instrument 64862 --duration 15

Outputs a short summary and lists samples from both sources.
"""
import argparse
import os
import time
import threading
import requests

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None


IIFL_API_BASE = "https://api.iiflcapital.com/v1"


class MQTTCollector:
    def __init__(self, instrument_id, token, host='bridge.iiflcapital.com', port=8883):
        self.instrument_id = instrument_id
        self.token = token
        self.host = host
        self.port = port
        self.samples = []
        self.client = None
        self._stop = threading.Event()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0 or (hasattr(rc, 'value') and rc.value == 0):
            topic = f"prod/marketfeed/mw/v1/nsefo/{self.instrument_id}"
            client.subscribe([(topic, 0)])
            print(f"Subscribed MQTT -> {topic}")
        else:
            print("MQTT connect failed rc=", rc)

    def _on_message(self, client, userdata, message):
        payload = message.payload
        if len(payload) >= 66:
            # Parse per official IIFL docs: bytes 0-3 = LTP, bytes 58-61 = priceDivisor
            import struct
            ltp_raw = struct.unpack_from('<i', payload, 0)[0]
            price_divisor = struct.unpack_from('<i', payload, 58)[0]
            divisor = float(price_divisor) if price_divisor > 0 else 100.0
            ltp = ltp_raw / divisor
            ts = time.time()
            self.samples.append((ts, ltp))

    def start(self):
        if mqtt is None:
            raise RuntimeError("paho-mqtt not installed")
        username = 'mqttuser'
        password = f"OPENID~~{self.token}~"
        self.client = mqtt.Client(protocol=mqtt.MQTTv311, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.client.tls_set(cert_reqs=None)
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


def poll_iifl_marketquotes(instrument_id, token, interval=1.0, duration=15):
    url = IIFL_API_BASE + '/marketdata/marketquotes'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = [{'exchange': 'NSEFO', 'instrumentId': str(instrument_id)}]
    samples = []
    end_t = time.time() + duration
    while time.time() < end_t:
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=5)
            if r.status_code == 200:
                j = r.json()
                res = j.get('result') or j.get('data') or []
                if res and isinstance(res, list):
                    item = res[0]
                    ltp = item.get('ltp')
                    samples.append((time.time(), ltp))
                else:
                    samples.append((time.time(), None))
            else:
                samples.append((time.time(), None))
        except Exception:
            samples.append((time.time(), None))
        time.sleep(interval)
    return samples


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--instrument', required=True, help='Instrument ID (numeric)')
    p.add_argument('--duration', type=int, default=15, help='Seconds to record')
    p.add_argument('--mqtt-host', default='bridge.iiflcapital.com')
    p.add_argument('--mqtt-port', type=int, default=8883)
    args = p.parse_args()

    token = os.getenv('IIFL_SESSION_TOKEN')
    if not token:
        print('IIFL_SESSION_TOKEN not set in environment. Export it (from .env) and retry.')
        return

    inst = args.instrument
    duration = args.duration

    collector = MQTTCollector(inst, token, host=args.mqtt_host, port=args.mqtt_port)
    try:
        collector.start()
    except Exception as e:
        print('Failed to start MQTT collector:', e)
        collector = None

    print(f'Polling IIFL marketquotes for instrument {inst} for {duration}s...')
    api_samples = poll_iifl_marketquotes(inst, token, interval=1.0, duration=duration)

    if collector:
        collector.stop()

    print('\n=== Results ===')
    print(f'Instrument: {inst}  Duration: {duration}s')

    print('\nAPI samples:')
    for ts, l in api_samples:
        print(f"  {time.strftime('%H:%M:%S', time.localtime(ts))}  LTP={l}")

    if collector and collector.samples:
        print('\nMQTT samples:')
        for ts, l in collector.samples:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(ts))}  LTP={l}")
    else:
        print('\nNo MQTT samples captured')

    api_vals = [v for _, v in api_samples if v is not None]
    mqtt_vals = [v for _, v in (collector.samples if collector else [])]

    if api_vals and mqtt_vals:
        avg_api = sum(api_vals) / len(api_vals)
        avg_mqtt = sum(mqtt_vals) / len(mqtt_vals)
        diff = avg_mqtt - avg_api
        print('\nSummary:')
        print(f'  API avg LTP = {avg_api:.2f}  |  MQTT avg LTP = {avg_mqtt:.2f}  |  diff = {diff:.2f}')
    elif api_vals and not mqtt_vals:
        print('\nOnly API data available (no MQTT samples)')
    elif mqtt_vals and not api_vals:
        print('\nOnly MQTT samples available (API returned no LTPs)')
    else:
        print('\nNo samples available from either source')


if __name__ == '__main__':
    main()
