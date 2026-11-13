"""MeshCore side of the bridge.

Talks to a MeshCore *companion* node (e.g. Heltec V3) over USB serial using
the official ``meshcore`` Python library (companion protocol).

Responsibilities:
  * connect (with port fallback + supervised auto-reconnect)
  * ensure the configured #hashtag channel exists (same key derivation as
    the phone app: secret = SHA256("#name")[:16])
  * subscribe to channel messages and forward them to a callback
  * send outgoing channel messages
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Awaitable, Callable, Optional, Sequence

from meshcore import EventType, MeshCore

logger = logging.getLogger("mcbridge.meshcore")

# Callback: (message text, sender timestamp, channel index)
MessageCallback = Callable[[str, int, int], Awaitable[None]]

_RECONNECT_MIN_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0


class MeshCoreLinkError(RuntimeError):
    pass


class MeshCoreLink:
    def __init__(
        self,
        ports: Sequence[str],
        hashtag: str,
        on_message: MessageCallback,
    ):
        self._ports = [p for p in ports if p]
        if not self._ports:
            raise ValueError("at least one serial port candidate required")
        self._hashtag = hashtag.lstrip("#").strip() or "bitchat"
        self._on_message = on_message

        self._mc: Optional[MeshCore] = None
        self._channel_idx: Optional[int] = None
        self._stopping = False
        self._lost = asyncio.Event()
        # last LoRa send timestamp (whole seconds), kept strictly increasing
        # so identical texts sent within one second aren't collapsed by the
        # firmware's (timestamp + text) flood dedup
        self._last_send_ts: int = 0

    # ------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        """Supervised connection loop: connect, wait for loss, reconnect."""
        delay = _RECONNECT_MIN_DELAY
        while not self._stopping:
            try:
                connected = await self._connect_any_port()
            except Exception as exc:
                logger.warning("MeshCore connection attempt failed: %s", exc)
                connected = False

            if self._stopping:
                break

            if connected:
                delay = _RECONNECT_MIN_DELAY
                self._lost.clear()
                await self._lost.wait()
                if self._stopping:
                    break
                logger.warning("MeshCore link lost; reconnecting...")
                await self._cleanup()
            else:
                logger.debug(
                    "MeshCore node not reachable; retrying in %.0fs", delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)
        await self._cleanup()

    async def stop(self) -> None:
        self._stopping = True
        self._lost.set()

    # ------------------------------------------------------------ connecting
    async def _connect_any_port(self) -> bool:
        for port in self._ports:
            if self._stopping:
                return False
            logger.info("Connecting to MeshCore node on %s ...", port)
            mc = None
            try:
                mc = await MeshCore.create_serial(port)
            except Exception as exc:
                logger.debug("create_serial(%s) failed: %s", port, exc)
                continue
            if mc is None:
                logger.debug("No companion response on %s", port)
                continue
            try:
                await self._setup(mc, port)
            except Exception as exc:
                logger.warning("Setup on %s failed: %s", port, exc)
                try:
                    await mc.disconnect()
                except Exception:
                    pass
                continue
            self._mc = mc
            return True
        return False

    async def _setup(self, mc: MeshCore, port: str) -> None:
        mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)

        res = await mc.commands.send_device_query()
        if res.type == EventType.ERROR:
            raise MeshCoreLinkError(f"device query failed: {res.payload}")
        max_channels = int(res.payload.get("max_channels", 8) or 8)

        self._channel_idx = await self._ensure_channel(mc, max_channels)

        mc.subscribe(
            EventType.CHANNEL_MSG_RECV,
            self._handle_channel_msg,
            attribute_filters={"channel_idx": self._channel_idx},
        )
        await mc.start_auto_message_fetching()

        node_name = (mc.self_info or {}).get("name") or "unknown"
        logger.info(
            "MeshCore link ready on %s (node '%s'); bridging channel #%s at slot %d",
            port,
            node_name,
            self._hashtag,
            self._channel_idx,
        )

    def _on_disconnected(self, event) -> None:
        reason = (event.payload or {}).get("reason", "unknown")
        logger.warning("MeshCore disconnected (%s)", reason)
        self._lost.set()

    # ------------------------------------------------------------- channels
    async def _ensure_channel(self, mc: MeshCore, max_channels: int) -> int:
        """Find a slot for the configured hashtag channel; create if missing."""
        want_name = f"#{self._hashtag}"
        secret = hashlib.sha256(want_name.encode("utf-8")).digest()[:16]

        first_empty: Optional[int] = None
        for idx in range(max_channels):
            res = await mc.commands.get_channel(idx)
            if res.type == EventType.ERROR:
                logger.debug("get_channel(%d) error: %s", idx, res.payload)
                continue
            payload = res.payload or {}
            name = (payload.get("channel_name") or "").strip().strip("\x00")
            sec = payload.get("channel_secret")
            if isinstance(sec, str):
                try:
                    sec = bytes.fromhex(sec)
                except ValueError:
                    sec = None

            if sec == secret:
                logger.info(
                    "Channel #%s already present in slot %d ('%s')",
                    self._hashtag,
                    idx,
                    name or want_name,
                )
                return idx

            if first_empty is None and not name and (sec is None or not any(sec)):
                first_empty = idx

        if first_empty is None:
            raise MeshCoreLinkError(
                f"No free channel slot to join {want_name} "
                f"(device has {max_channels} slots)"
            )

        res = await mc.commands.set_channel(first_empty, want_name)
        if res.type == EventType.ERROR:
            raise MeshCoreLinkError(f"set_channel failed: {res.payload}")
        logger.info("Joined channel %s in slot %d", want_name, first_empty)
        return first_empty

    # -------------------------------------------------------------- receive
    async def _handle_channel_msg(self, event) -> None:
        msg = event.payload or {}
        text = (msg.get("text") or "").strip("\x00").strip()
        timestamp = int(msg.get("sender_timestamp", 0) or 0)
        if not text:
            return
        if self._channel_idx is None:
            return
        try:
            await self._on_message(text, timestamp, self._channel_idx)
        except Exception:
            logger.exception("on_message callback failed")

    # ----------------------------------------------------------------- send
    async def send(self, text: str) -> bool:
        mc, idx = self._mc, self._channel_idx
        if mc is None or idx is None or not mc.is_connected:
            logger.warning("MeshCore not connected; dropping message: %r", text[:60])
            return False
        # Whole-second timestamps: keep them strictly increasing so two
        # identical texts sent within one second aren't collapsed by the
        # firmware's (timestamp + text) flood dedup.
        now = int(time.time())
        ts = now if now > self._last_send_ts else self._last_send_ts + 1
        self._last_send_ts = ts
        try:
            res = await mc.commands.send_chan_msg(idx, text, timestamp=ts)
        except Exception as exc:
            logger.error("send_chan_msg raised: %s", exc)
            self._lost.set()
            return False
        if res.type == EventType.ERROR:
            logger.error("send_chan_msg error: %s", res.payload)
            return False
        return True

    # -------------------------------------------------------------- cleanup
    async def _cleanup(self) -> None:
        if self._mc is not None:
            try:
                await self._mc.disconnect()
            except Exception:
                pass
            self._mc = None
        self._channel_idx = None
