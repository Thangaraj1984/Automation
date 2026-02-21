"""IIFL Capital Markets MQTT Feed Client.

Connects to bridge.iiflcapital.com:8883 using MQTT over TLS (paho-mqtt).
Subscribes to both Market Watch (MW) and Open Interest (OI) topics
on the same MQTT connection — no separate REST API polling needed.

Port 8883 is the standard MQTT-over-TLS port.
"""
import asyncio
import ssl
import re
import json
import base64
import threading
import structlog
import paho.mqtt.client as mqtt
from datetime import datetime
from typing import Callable, Awaitable, Optional
from . import parse_binary_packet, parse_oi_packet, MarketTick, OITick
from ..config import Config

log = structlog.get_logger()

# MQTT topic prefixes (from BridgePy connector.py)
MW_TOPIC = "prod/marketfeed/mw/v1/"
OI_TOPIC = "prod/marketfeed/oi/v1/"
INDEX_TOPIC = "prod/marketfeed/index/v1/"


def _extract_username_from_jwt(token: str) -> str:
    """Extract preferred_username from JWT token payload (same as BridgePy)."""
    try:
        parts = token.split(".")
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
        return claims.get("preferred_username", "tester")
    except Exception:
        return "tester"


def parse_topic_inst_id(topic: str) -> int | None:
    """Extract numeric instrument id from MQTT topic path like '.../nsefo/35005'.

    Returns None if not present or not an int.
    """
    try:
        parts = re.split(r"v1/", topic)
        if len(parts) >= 2:
            inst_id_str = parts[1].split("/")[-1]
            return int(inst_id_str)
    except Exception:
        return None
    return None


class MQTTFeedClient:
    """IIFL Capital Markets MQTT feed client.

    Subscribes to both Market Watch (price ticks) and Open Interest
    on the same MQTT connection, matching the BridgePy architecture.
    """

    def __init__(
        self,
        session_token: str,
        on_tick: Callable[[MarketTick], Awaitable[None]],
        on_oi: Callable[[OITick], Awaitable[None]],
    ):
        """
        Args:
            session_token: JWT token from IIFL authentication.
            on_tick: Async callback for each parsed MarketTick (MW feed).
            on_oi: Async callback for each parsed OITick (OI feed).
        """
        self.session_token = session_token
        self.on_tick = on_tick
        self.on_oi = on_oi
        self._subscriptions: list[str] = []
        self._running = False
        self._connected = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._tick_count = 0
        self._oi_count = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Create MQTT client matching BridgePy setup
        client_id = "nseIngestor" + datetime.now().strftime("%d%m%y%H%M%S")
        self._mqtt = mqtt.Client(
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            reconnect_on_failure=False,
        )

        # TLS setup matching BridgePy: TLSv1.2, no cert verification
        self._mqtt.tls_set(
            ca_certs=None, certfile=None, keyfile=None,
            cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLSv1_2,
            ciphers=None,
        )
        self._mqtt.tls_insecure_set(True)
        self._mqtt.keepalive = 20

        # Authentication: username from JWT, password is "OPENID~~{token}~"
        username = _extract_username_from_jwt(session_token)
        self._mqtt.username_pw_set(
            username=username,
            password=f"OPENID~~{session_token}~",
        )

        # Set callbacks
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message
        self._mqtt.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Called when MQTT connection is established."""
        rc_val = rc.value if hasattr(rc, 'value') else rc
        if rc_val == 0:
            self._connected = True
            self._reconnect_delay = 1
            log.info("mqtt_connected", host=Config.IIFL_WS_HOST)

            # Subscribe to stored subscriptions on connect/reconnect
            if self._subscriptions:
                self._do_subscribe(self._subscriptions)
        else:
            log.error("mqtt_connect_failed", rc=rc_val, reason=str(rc))

    def _on_disconnect(self, client, userdata, flags=None, rc=None, properties=None):
        """Called when MQTT connection is lost."""
        self._connected = False
        rc_val = rc.value if hasattr(rc, 'value') else (rc or 0)
        log.warning("mqtt_disconnected", rc=rc_val)

    def _on_message(self, client, userdata, message):
        """Called for each received MQTT message. Runs in MQTT thread.

        Routes messages based on topic prefix:
        - prod/marketfeed/mw/v1/...  → MW price tick handler
        - prod/marketfeed/oi/v1/...  → OI handler
        """
        try:
            payload = message.payload
            topic = message.topic

            if MW_TOPIC in topic:
                # Market Watch tick
                # Extract correct instrument ID from MQTT topic
                # Topic format: prod/marketfeed/mw/v1/nsefo/48097
                # The binary payload's first 4 bytes contain an exchange token,
                # NOT the IIFL instrument ID, so we must use the topic.
                parts = re.split(r"v1/", topic)
                if len(parts) >= 2:
                    inst_id_str = parts[1].split("/")[-1]
                    try:
                        topic_inst_id = int(inst_id_str)
                    except ValueError:
                        return

                    tick = parse_binary_packet(bytes(payload))
                    if tick and (tick.ltp > 0 or tick.total_traded_volume > 0):
                        log.debug("mqtt_tick_parsed",
                                  topic_inst_id=topic_inst_id,
                                  ltp=tick.ltp)

                        # Override the binary payload's instrument_id with the
                        # correct one from the MQTT topic
                        tick.instrument_id = topic_inst_id
                        self._tick_count += 1
                        if self._loop and self._loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                self.on_tick(tick), self._loop
                            )
                        if self._tick_count % 10000 == 0:
                            log.info("mqtt_tick_milestone",
                                     total_ticks=self._tick_count,
                                     total_oi=self._oi_count)

            elif OI_TOPIC in topic:
                # Open Interest update
                # Extract instrument ID from topic: prod/marketfeed/oi/v1/nsefo/35005
                parts = re.split(r"v1/", topic)
                if len(parts) >= 2:
                    inst_path = parts[1]  # "nsefo/35005"
                    inst_id_str = inst_path.split("/")[-1]
                    try:
                        inst_id = int(inst_id_str)
                    except ValueError:
                        return

                    oi_tick = parse_oi_packet(bytes(payload), inst_id)
                    if oi_tick:
                        self._oi_count += 1
                        if self._loop and self._loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                self.on_oi(oi_tick), self._loop
                            )
                        if self._oi_count % 5000 == 0:
                            log.info("mqtt_oi_milestone",
                                     total_oi=self._oi_count)

        except Exception as e:
            log.error("mqtt_message_error", error=str(e))

    def _do_subscribe(self, instruments: list[str]):
        """Subscribe to MQTT topics for both MW and OI feeds.

        For each instrument token (e.g. "nsefo/35005"), subscribes to:
        - "prod/marketfeed/mw/v1/nsefo/35005" (price ticks)
        - "prod/marketfeed/oi/v1/nsefo/35005" (OI updates)
        """
        batch_size = 512  # Half of 1024 limit since we subscribe 2 topics per instrument
        for i in range(0, len(instruments), batch_size):
            batch = instruments[i:i + batch_size]

            # Build combined MW + OI topic list
            topics = []
            for inst in batch:
                topics.append((MW_TOPIC + inst, 0))
                topics.append((OI_TOPIC + inst, 0))

            self._mqtt.subscribe(topics)
            log.info(
                "mqtt_subscribed",
                batch_num=i // batch_size + 1,
                instruments=len(batch),
                topics=len(topics),
                total_instruments=len(instruments),
            )

    async def subscribe(self, instruments: list[str]):
        """Subscribe to market feed for given instruments (async wrapper)."""
        self._subscriptions = instruments
        if self._connected:
            self._do_subscribe(instruments)

    async def unsubscribe(self, instruments: list[str]):
        """Unsubscribe from market feed."""
        if self._connected:
            topics = []
            for inst in instruments:
                topics.append(MW_TOPIC + inst)
                topics.append(OI_TOPIC + inst)
            self._mqtt.unsubscribe(topics)
            log.info("mqtt_unsubscribed", count=len(instruments))

    async def connect(self):
        """Establish MQTT connection to IIFL Bridge."""
        log.info("mqtt_connecting", host=Config.IIFL_WS_HOST, port=Config.IIFL_WS_PORT)
        try:
            result = self._mqtt.connect(
                host=Config.IIFL_WS_HOST,
                port=Config.IIFL_WS_PORT,
                keepalive=20,
            )
            rc_val = result.value if hasattr(result, 'value') else result
            if rc_val == 0:
                self._mqtt.loop_start()
                # Wait for connection to establish
                for _ in range(50):  # 5 seconds max
                    if self._connected:
                        break
                    await asyncio.sleep(0.1)
                if not self._connected:
                    raise RuntimeError("MQTT connection timeout - on_connect not received")
            else:
                raise RuntimeError(f"MQTT connect returned rc={rc_val}")
        except Exception as e:
            log.error("mqtt_connect_error", error=str(e))
            raise

    async def run(self):
        """Main loop: connect, subscribe, and keep alive."""
        self._running = True
        self._loop = asyncio.get_event_loop()

        while self._running:
            try:
                await self.connect()

                if self._subscriptions:
                    await self.subscribe(self._subscriptions)

                # Keep running while connected
                while self._running and self._connected:
                    await asyncio.sleep(1)

                # If we get here, we disconnected
                if self._running:
                    self._mqtt.loop_stop()

            except Exception as e:
                log.error("mqtt_error", error=str(e), type=type(e).__name__)
                try:
                    self._mqtt.loop_stop()
                except Exception:
                    pass

            if self._running:
                log.info("mqtt_reconnecting", delay=self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    async def stop(self):
        """Gracefully stop the MQTT client."""
        self._running = False
        try:
            self._mqtt.disconnect()
            self._mqtt.loop_stop()
        except Exception:
            pass
        log.info("mqtt_stopped", total_ticks=self._tick_count, total_oi=self._oi_count)

    @property
    def is_connected(self) -> bool:
        return self._connected
