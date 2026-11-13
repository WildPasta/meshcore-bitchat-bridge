"""Bitchat BLE side of the bridge.

The bridge acts as a BLE *central*: it continuously scans for phones running
the Bitchat app (which advertise the Bitchat service UUID), connects to them,
performs the signed ANNOUNCE handshake, and then exchanges signed packets.

Ported and hardened from GigaProjects/meshtastic-bitchat-bridge (mesh-bit.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict, deque
from typing import Awaitable, Callable, Dict, Optional, Set

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import bitchat_packets as bp
from .identity import BridgeIdentity
from .router import sanitize_nickname, utf8_chunk

logger = logging.getLogger("mcbridge.bitchat")

# Callback: (nickname or None, sender_id hex, message text)
MessageCallback = Callable[[Optional[str], str, str], Awaitable[None]]

_SCAN_TIMEOUT = 3.0
_SCAN_PAUSE = 0.5
_SCAN_ERROR_BACKOFF_MAX = 60.0
_CONNECT_TIMEOUT = 10.0
_CONNECT_RETRIES = 3
_RETRY_DELAY = 2.0
_FAILED_COOLDOWN = 300.0  # 5 min blacklist for devices that keep failing
_DEDUP_HISTORY = 200
_NICKNAME_DEBOUNCE = 1.0
_BROADCAST_RECIPIENT = b"\xff" * 8
_MAX_PEERS = 500  # LRU bound for peer_nicknames / peer_keys


class BitchatLink:
    """BLE central role Bitchat peer."""

    def __init__(
        self,
        identity: BridgeIdentity,
        nickname: str,
        on_message: MessageCallback,
    ):
        self.identity = identity
        # Sanitize our own nickname like a peer's: it lands in an ANNOUNCE
        # TLV, and the 15-char cap keeps the ANNOUNCE payload (70 + len
        # bytes) safely under the apps' 100-byte compression threshold (see
        # bitchat_packets.SAFE_UNCOMPRESSED_PAYLOAD).
        clean = sanitize_nickname(nickname)
        if clean != nickname:
            logger.debug("Bridge nickname %r sanitized to %r", nickname, clean)
        self.nickname = clean or "bridge"
        self.on_message = on_message

        self.connected_clients: Dict[str, BleakClient] = {}
        self._connecting: Set[str] = set()
        self._failed_devices: Dict[str, float] = {}
        # sender hex -> nickname / Ed25519 signing key; simple LRU bounded
        # peers so a long run in a busy area cannot grow them forever
        self.peer_nicknames: OrderedDict[str, str] = OrderedDict()
        self.peer_keys: OrderedDict[str, bytes] = OrderedDict()
        self._processed_packets: deque = deque(maxlen=_DEDUP_HISTORY)
        self._nickname_tasks: Dict[str, asyncio.Task] = {}
        self._stopping = False
        # strong references to fire-and-forget tasks (asyncio only keeps weak
        # refs; unreferenced tasks can be garbage-collected mid-execution)
        self._bg_tasks: Set[asyncio.Task] = set()
        # last BLE packet timestamp we emitted (ms), kept strictly increasing
        # so phones never dedup two distinct bridge packets as one
        self._last_tx_ms: int = 0

    # -------------------------------------------------------------- helpers
    def _spawn(self, coro) -> asyncio.Task:
        """Create a tracked background task; cancelled in stop()."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _next_timestamp_ms(self) -> int:
        """Strictly-increasing millisecond timestamp for outgoing packets.

        Phones de-duplicate by (sender id, timestamp); two packets stamped in
        the same millisecond would make the second one invisible.
        """
        now = int(time.time() * 1000)
        self._last_tx_ms = max(now, self._last_tx_ms + 1)
        return self._last_tx_ms

    # ------------------------------------------------------------------ scan
    async def run(self) -> None:
        """Continuously scan for Bitchat peers and connect to them."""
        logger.info(
            "Scanning for Bitchat devices (bridge nick '%s', id %s)...",
            self.nickname,
            self.identity.short_id,
        )
        backoff = 1.0
        while not self._stopping:
            try:
                devices = await BleakScanner.discover(timeout=_SCAN_TIMEOUT, return_adv=True)
                backoff = 1.0
                for device, adv in devices.values():
                    if bp.BITCHAT_SERVICE_UUID.lower() not in adv.service_uuids:
                        continue

                    address = device.address
                    # cooldown for devices that failed before
                    if address in self._failed_devices:
                        if time.time() - self._failed_devices[address] < _FAILED_COOLDOWN:
                            continue
                        del self._failed_devices[address]

                    if (
                        address not in self.connected_clients
                        and address not in self._connecting
                    ):
                        logger.debug("Found Bitchat peer: %s", address)
                        self._connecting.add(address)
                        self._spawn(self._connect_client(device))
            except Exception as exc:
                # A dead adapter (hci0 down) makes the bridge look alive from
                # the MeshCore side; surface it loudly with exponential backoff
                # instead of DEBUG + fixed 1 s.
                logger.warning(
                    "BLE scanner error: %s (retrying in %.0fs)", exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _SCAN_ERROR_BACKOFF_MAX)

            await asyncio.sleep(_SCAN_PAUSE)

    # -------------------------------------------------------------- connect
    async def _connect_client(self, device: BLEDevice) -> None:
        address = device.address
        try:
            await self._connect_client_inner(device)
        finally:
            # CancelledError is not an Exception; without try/finally a
            # cancelled connect() would leave the address stuck in
            # _connecting forever (peer never reconnectable until restart).
            self._connecting.discard(address)

    async def _connect_client_inner(self, device: BLEDevice) -> None:
        address = device.address
        for attempt in range(_CONNECT_RETRIES):
            if self._stopping:
                return
            client = BleakClient(
                address,
                timeout=_CONNECT_TIMEOUT,
                disconnected_callback=self._on_client_disconnect,
            )
            try:
                logger.debug(
                    "Connection attempt %d/%d to %s", attempt + 1, _CONNECT_RETRIES, address
                )
                await client.connect()
                if not client.is_connected:
                    raise RuntimeError("client reports not connected")

                if self._stopping:
                    # stop() raced with our connect(); don't register it
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    return

                logger.info("Connected to Bitchat peer %s", address)
                self.connected_clients[address] = client

                # notifications BEFORE the handshake so the reply isn't lost
                await client.start_notify(
                    bp.BITCHAT_TX_CHAR_UUID, self._make_notification_handler(address)
                )

                announce = bp.build_announce_payload(self.identity, self.nickname)
                packet = bp.build_packet(
                    self.identity,
                    bp.PACKET_TYPE_ANNOUNCE,
                    announce,
                    recipient_id=_BROADCAST_RECIPIENT,
                    timestamp_ms=self._next_timestamp_ms(),
                )
                await client.write_gatt_char(bp.BITCHAT_RX_CHAR_UUID, packet, response=True)
                logger.debug("Handshake ANNOUNCE sent to %s", address)
                return
            except Exception as exc:
                error_msg = str(exc)
                logger.debug("Connection error (%s): %s", address, error_msg)
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass

                transient = "InProgress" in error_msg or "br-connection-canceled" in error_msg
                if transient and attempt < _CONNECT_RETRIES - 1:
                    await asyncio.sleep(_RETRY_DELAY * (2**attempt))
                    continue
                break  # fatal error: stop retrying

        logger.debug("Giving up on %s for now", address)
        self._failed_devices[address] = time.time()
        self.connected_clients.pop(address, None)

    def _on_client_disconnect(self, client: BleakClient) -> None:
        address = client.address
        # The callback is registered before connect() (and once per retry
        # attempt), so bleak also fires it for probes that never established
        # a connection — rotating privacy MACs make those frequent, and each
        # used to log "disconnected" at INFO although "Connected to ..." never
        # fired. Only a peer that actually connected stays at INFO.
        if self.connected_clients.pop(address, None) is not None:
            logger.info("Bitchat peer %s disconnected", address)
        else:
            logger.debug("Probe %s ended without connecting", address)
        self._connecting.discard(address)

    # ------------------------------------------------------------ receive
    def _make_notification_handler(self, address: str):
        def handler(_sender_handle: int, data: bytearray) -> None:
            self._spawn(self._process_packet(bytes(data), address))

        return handler

    async def _process_packet(self, data: bytes, address: str) -> None:
        try:
            packet = bp.parse_packet(data)
        except bp.PacketError as exc:
            logger.debug("Unparseable packet from %s: %s", address, exc)
            return
        except Exception:
            logger.exception("Unexpected error parsing packet from %s", address)
            return

        if packet.packet_type in bp.FRAGMENT_TYPES:
            # The apps fragment payloads > 512 bytes (type 0x20, legacy
            # 0x05/0x06/0x07). We do not reassemble; drop with a note.
            logger.debug(
                "Dropping fragmented packet (type 0x%02x) from %s — "
                "fragment reassembly not supported",
                packet.packet_type,
                address,
            )
            return

        # de-dup by (type, sender, timestamp): relayed echoes repeat the
        # exact frame, but a peer legitimately sends an ANNOUNCE and a
        # MESSAGE within the same millisecond (the on-connect sequence) —
        # those must not collide
        packet_id = (
            packet.packet_type,
            packet.sender_id,
            packet.timestamp_ms.to_bytes(8, "big"),
        )
        if packet_id in self._processed_packets:
            return
        self._processed_packets.append(packet_id)

        sender_hex = packet.sender_id.hex()
        short_id = sender_hex[-4:]

        if packet.packet_type == bp.PACKET_TYPE_MESSAGE:
            if not packet.is_broadcast:
                # binding decision: relay broadcast (public) chat only;
                # private DMs are never forwarded to the mesh
                logger.debug(
                    "Dropping non-broadcast MESSAGE from %s (DM relay disabled)",
                    short_id,
                )
                return
            if sender_hex == self.identity.my_id.hex():
                logger.debug("Ignoring echo of our own packet")
                return
            key = self.peer_keys.get(sender_hex)
            if key is None:
                logger.warning(
                    "Dropping MESSAGE from unknown sender %s "
                    "(no verified ANNOUNCE seen; cannot verify signature)",
                    short_id,
                )
                return
            if packet.signature is None:
                logger.warning("Dropping unsigned MESSAGE from %s", short_id)
                return
            if not bp.verify_signature(packet, key):
                # key is bound to this sender id, so a bad signature means a
                # forged frame — keep the key, drop the packet
                logger.warning(
                    "MESSAGE signature verification FAILED from %s — "
                    "possible spoofing attempt; packet dropped",
                    short_id,
                )
                return
            nickname = self.peer_nicknames.get(sender_hex)
            logger.info("(BLE -> Bridge) %s: %s", nickname or short_id, packet.text)
            try:
                await self.on_message(nickname, sender_hex, packet.text)
            except Exception:
                logger.exception("on_message callback failed")

        elif packet.packet_type == bp.PACKET_TYPE_ANNOUNCE:
            tlvs = bp.parse_announce_tlvs(packet.payload)
            key = tlvs.get(bp.TLV_SIGNING_KEY)
            if key is None or len(key) != 32:
                logger.debug(
                    "Dropping ANNOUNCE from %s (missing/invalid signing key TLV)",
                    short_id,
                )
                return
            noise_key = tlvs.get(bp.TLV_NOISE_KEY)
            if noise_key is None or len(noise_key) != 32:
                logger.debug(
                    "Dropping ANNOUNCE from %s (missing/invalid noise key TLV)",
                    short_id,
                )
                return
            # Peer id = SHA256(noise key)[:8] — NOT the signing key. Official
            # apps derive the id from the Noise/X25519 static key (WHITEPAPER
            # §3) and reject announces that don't match it; do the same so an
            # ANNOUNCE cannot claim an id the sender doesn't own.
            if hashlib.sha256(noise_key).digest()[:8] != packet.sender_id:
                logger.warning(
                    "Dropping ANNOUNCE from %s: noise key hash does not "
                    "match sender id (possible spoofing)",
                    short_id,
                )
                return
            if packet.signature is None:
                # README documents a *signed* ANNOUNCE handshake; official
                # apps always sign. Don't store keys from unsigned frames.
                logger.debug("Dropping unsigned ANNOUNCE from %s", short_id)
                return
            if not bp.verify_signature(packet, key):
                logger.warning(
                    "Dropping ANNOUNCE from %s: signature verification failed",
                    short_id,
                )
                return
            self._remember_key(sender_hex, key)

            nickname = tlvs.get(bp.TLV_NICKNAME)
            if nickname is None:
                return
            nickname = sanitize_nickname(
                nickname.decode("utf-8", errors="ignore")
            )
            if not nickname or self.peer_nicknames.get(sender_hex) == nickname:
                return
            self.peer_nicknames[sender_hex] = nickname
            self.peer_nicknames.move_to_end(sender_hex)
            while len(self.peer_nicknames) > _MAX_PEERS:
                self.peer_nicknames.popitem(last=False)

            # Bitchat streams partial nicknames while a peer types; debounce logs
            old_task = self._nickname_tasks.get(sender_hex)
            if old_task and not old_task.done():
                old_task.cancel()
            task = asyncio.create_task(
                self._log_nickname_later(short_id, sender_hex)
            )
            task.add_done_callback(self._forget_nickname_task)
            self._nickname_tasks[sender_hex] = task

    def _remember_key(self, sender_hex: str, key: bytes) -> None:
        self.peer_keys[sender_hex] = key
        self.peer_keys.move_to_end(sender_hex)
        while len(self.peer_keys) > _MAX_PEERS:
            self.peer_keys.popitem(last=False)

    def _forget_nickname_task(self, task: asyncio.Task) -> None:
        """Done callback: evict finished entries from _nickname_tasks."""
        for sender_hex, pending in list(self._nickname_tasks.items()):
            if pending is task:
                self._nickname_tasks.pop(sender_hex, None)

    async def _log_nickname_later(self, short_id: str, sender_hex: str) -> None:
        try:
            await asyncio.sleep(_NICKNAME_DEBOUNCE)
        except asyncio.CancelledError:
            return
        final = self.peer_nicknames.get(sender_hex)
        if final:
            logger.info("Peer %s is now known as '%s'", short_id, final)

    # -------------------------------------------------------------- send
    async def broadcast(self, text: str) -> None:
        """Send a chat message to every connected Bitchat peer."""
        if self._stopping or not text:
            return
        pieces = [text]
        if len(text.encode("utf-8")) > bp.SAFE_UNCOMPRESSED_PAYLOAD:
            # Backstop only — the router normally chunks to stay under the
            # apps' compression threshold (see bitchat_packets
            # .SAFE_UNCOMPRESSED_PAYLOAD); an uncompressed packet this large
            # would fail signature verification on phones.
            logger.warning(
                "Oversized BLE payload (%d bytes) — splitting to stay "
                "under the apps' compression threshold",
                len(text.encode("utf-8")),
            )
            pieces = utf8_chunk(text, bp.SAFE_UNCOMPRESSED_PAYLOAD)
        sent = 0
        for piece in pieces:
            packet = bp.build_packet(
                self.identity,
                bp.PACKET_TYPE_MESSAGE,
                piece.encode("utf-8"),
                timestamp_ms=self._next_timestamp_ms(),
            )
            for client in list(self.connected_clients.values()):
                try:
                    if client.is_connected:
                        await client.write_gatt_char(
                            bp.BITCHAT_RX_CHAR_UUID, packet, response=True
                        )
                        sent += 1
                except Exception as exc:
                    logger.debug(
                        "Broadcast to %s failed: %s", client.address, exc
                    )
        if sent:
            logger.debug("Broadcast delivered to %d peer(s)", sent)
        else:
            logger.debug("No connected Bitchat peers; message not delivered over BLE")

    # -------------------------------------------------------------- stop
    async def stop(self) -> None:
        self._stopping = True
        logger.info("Disconnecting Bitchat peers...")
        for client in list(self.connected_clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        self.connected_clients.clear()
        for task in self._nickname_tasks.values():
            task.cancel()
        self._nickname_tasks.clear()
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
