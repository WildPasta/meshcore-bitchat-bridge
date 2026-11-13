"""Message routing rules between Bitchat and MeshCore.

Formatting conventions
----------------------
* Bitchat -> MeshCore:  ``bitchat/{nick}: {text}``  — follows MeshCore's
  group-chat convention (``<sender name>: <body>``, see docs/payloads.md),
  so the MeshCore app renders it as a normal channel message. The
  ``bitchat/`` marker lets any bridge recognise (and drop) its own traffic
  coming back, preventing loops.
* MeshCore -> Bitchat:  ``[Mesh: {sender}] {body}`` (or ``[Mesh] {text}``
  when no ``sender: `` prefix is present).
"""

from __future__ import annotations

import hashlib
import time
from typing import Callable, Dict, List, Optional, Tuple

from .bitchat_packets import SAFE_UNCOMPRESSED_PAYLOAD

# Firmware: MAX_TEXT_LEN = 10 * CIPHER_BLOCK_SIZE = 160 bytes
MAX_MESH_TEXT = 160

BITCHAT_ORIGIN_PREFIX = "bitchat/"

# BLE -> LoRa flood control: a peer may send at most RATE_LIMIT_BURST
# messages back-to-back, refilling at RATE_LIMIT_PER_MINUTE thereafter.
RATE_LIMIT_BURST = 3
RATE_LIMIT_PER_MINUTE = 6.0

# Dedup window: bounded by *receive* time (monotonic), not by the sender's
# own timestamp — dead-clock nodes (sender_timestamp=0) would otherwise be
# deduped into silence forever.
DEDUP_WINDOW_SECONDS = 30.0
_SEEN_HISTORY = 256

# Cap on relayed BLE text: a 64 KiB payload would otherwise chunk into ~500
# LoRa frames (channel flood). Oversized input is truncated with a marker.
MAX_RELAY_BYTES = 1000
_TRUNCATION_MARKER = " [truncated]"
# Matches the app's MAX_NICKNAME_LENGTH. Nicknames arrive in untrusted ANNOUNCE
# TLVs (up to 255 bytes) and would push the "bitchat/{nick}: " prefix past the
# 160-byte LoRa budget.
MAX_NICKNAME_CHARS = 15


def sanitize_nickname(nickname: Optional[str]) -> str:
    """Strip control characters (< 0x20, 0x7f) and cap the length.

    Nicknames come from untrusted ANNOUNCE TLVs; a raw ``\\n`` inside the
    ``bitchat/{nick}: `` prefix would forge a second "sender: body" line in
    the LoRa text. Control chars are removed *before* the length cap.
    """
    label = "".join(
        ch for ch in (nickname or "") if ord(ch) >= 0x20 and ord(ch) != 0x7F
    )
    label = label.strip()
    if len(label) > MAX_NICKNAME_CHARS:
        label = label[:MAX_NICKNAME_CHARS]
    return label


def utf8_chunk(text: str, budget: int) -> list:
    """Word-wrap ``text`` into pieces of at most ``budget`` UTF-8 bytes."""
    if budget < 8:
        budget = 8
    chunks: list = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else current + " " + word
        if len(candidate.encode("utf-8")) <= budget:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(word.encode("utf-8")) <= budget:
            current = word
            continue
        # single word longer than the budget: hard-split on char boundaries
        part = ""
        for ch in word:
            if len((part + ch).encode("utf-8")) > budget:
                chunks.append(part)
                part = ch
            else:
                part += ch
        current = part
    if current:
        chunks.append(current)
    return chunks


class MessageRateLimiter:
    """Per-sender token bucket (BLE -> LoRa flood control).

    Each sender starts with ``burst`` tokens and refills at
    ``per_minute / 60`` tokens per second.
    """

    def __init__(
        self,
        burst: int = RATE_LIMIT_BURST,
        per_minute: float = RATE_LIMIT_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.capacity = float(burst)
        self.refill_per_second = per_minute / 60.0
        self._clock = clock
        self._tokens: Dict[str, float] = {}
        self._updated: Dict[str, float] = {}

    def allow(self, sender_id: str) -> bool:
        now = self._clock()
        tokens = min(
            self.capacity,
            self._tokens.get(sender_id, self.capacity)
            + (now - self._updated.get(sender_id, now)) * self.refill_per_second,
        )
        self._updated[sender_id] = now
        if tokens >= 1.0:
            self._tokens[sender_id] = tokens - 1.0
            return True
        self._tokens[sender_id] = tokens
        return False


class Router:
    def __init__(
        self,
        max_mesh_text: int = MAX_MESH_TEXT,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_mesh_text = max_mesh_text
        self._clock = clock
        # dedup key -> monotonic receive time
        self._mesh_seen: Dict[str, float] = {}

    # --------------------------------------------------------- BLE -> LoRa
    def bitchat_to_mesh(self, nickname, text) -> list:
        """Format (and chunk) a Bitchat message into MeshCore channel texts."""
        clean = " ".join(text.split())  # collapse whitespace/newlines
        if not clean:
            return []
        if len(clean.encode("utf-8")) > MAX_RELAY_BYTES:
            clean = (
                clean.encode("utf-8")[:MAX_RELAY_BYTES]
                .decode("utf-8", errors="ignore")
                .rstrip()
                + _TRUNCATION_MARKER
            )
        label = sanitize_nickname(nickname) or "peer"
        prefix = f"{BITCHAT_ORIGIN_PREFIX}{label}: "

        budget = self.max_mesh_text - len(prefix.encode("utf-8")) - 12
        pieces = utf8_chunk(clean, budget)
        if len(pieces) <= 1:
            return [prefix + (pieces[0] if pieces else "")]
        total = len(pieces)
        return [
            f"{prefix}({i}/{total}) {piece}" for i, piece in enumerate(pieces, 1)
        ]

    # --------------------------------------------------------- LoRa -> BLE
    def mesh_to_bitchat(self, text: str, timestamp: int, channel_idx: int = 0):
        """Format a MeshCore channel message for Bitchat; ``[]`` to drop."""
        return self.mesh_to_bitchat_with_reason(text, timestamp, channel_idx)[0]

    def mesh_to_bitchat_with_reason(
        self, text: str, timestamp: int, channel_idx: int = 0
    ) -> Tuple[List[str], Optional[str]]:
        """Like :meth:`mesh_to_bitchat`, but also returns the drop reason.

        Returns ``([], reason)`` when the message is dropped and
        ``(pieces, None)`` otherwise. Each piece is a complete BLE broadcast
        payload (``[Mesh: ...] ...``) of at most
        :data:`~mcbridge.bitchat_packets.SAFE_UNCOMPRESSED_PAYLOAD` bytes:
        official apps re-compress — and then fail to verify our signature
        on — any payload >= 100 bytes, so long bodies are split into
        ``(i/N)``-marked pieces instead.

        Reasons (for grepable logs): ``"empty"``, ``"loop-prevention marker"``,
        ``"duplicate"``. ``None`` reason means the message was relayed.
        """
        clean = text.strip().strip("\x00").strip()
        if not clean:
            return [], "empty"
        if clean.startswith(BITCHAT_ORIGIN_PREFIX):
            return [], "loop-prevention marker"

        key = hashlib.sha256(
            f"{channel_idx}|{timestamp}|{clean}".encode("utf-8")
        ).hexdigest()
        now = self._clock()
        self._expire_seen(now)
        if key in self._mesh_seen:
            return [], "duplicate"
        self._mesh_seen[key] = now
        self._cap_seen()

        sender, sep, body = clean.partition(": ")
        if (
            sep
            and sender
            and len(sender) <= 48
            and "\n" not in sender
            and "://" not in sender
        ):
            return self._ble_pieces(f"[Mesh: {sender}] ", body.strip()), None
        return self._ble_pieces("[Mesh] ", clean), None

    @staticmethod
    def _ble_pieces(prefix: str, body: str) -> List[str]:
        """Split ``prefix + body`` into BLE payloads under the size cap.

        Mirrors :meth:`bitchat_to_mesh`: ``(i/N)`` markers on multi-part
        output, word-wrapped on UTF-8 character boundaries. The 8-byte
        reserve covers the largest marker (" (99/99) ").
        """
        budget = SAFE_UNCOMPRESSED_PAYLOAD - len(prefix.encode("utf-8")) - 8
        pieces = utf8_chunk(body, budget)
        if len(pieces) <= 1:
            return [prefix + (pieces[0] if pieces else "")]
        total = len(pieces)
        return [
            f"{prefix}({i}/{total}) {piece}"
            for i, piece in enumerate(pieces, 1)
        ]

    def _expire_seen(self, now: float) -> None:
        """Drop dedup entries older than the receive-time window."""
        expired = [
            k for k, ts in self._mesh_seen.items()
            if now - ts >= DEDUP_WINDOW_SECONDS
        ]
        for k in expired:
            del self._mesh_seen[k]

    def _cap_seen(self) -> None:
        """Keep at most _SEEN_HISTORY entries (drop the oldest)."""
        if len(self._mesh_seen) > _SEEN_HISTORY:
            by_age = sorted(self._mesh_seen.items(), key=lambda kv: kv[1])
            for k, _ in by_age[: len(self._mesh_seen) - _SEEN_HISTORY]:
                del self._mesh_seen[k]
