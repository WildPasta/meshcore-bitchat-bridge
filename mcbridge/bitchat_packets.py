"""Bitchat binary packet protocol (wire format).

Ported from the proven implementation in GigaProjects/meshtastic-bitchat-bridge
(mesh-bit.py), which itself mirrors bitchat's BinaryProtocol.kt / MessagePadding.kt.

Packet layout (big-endian multi-byte fields):

    Ver(1) Type(1) TTL(1) TimestampMs(8) Flags(1) Len(2)   <- 14 byte header
    SenderId(8) [RecipientId(8)] Payload(Len) [Signature(64)]
    <PKCS-style padding to 256/512/1024/2048 bytes>

Signing subtlety (matches the Android app): the signature is computed over
the *padded* unsigned packet, with TTL forced to 0 and the HAS_SIGNATURE
flag cleared.
"""

from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass
from typing import Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

# --- BLE UUIDs ---
BITCHAT_SERVICE_UUID = "f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c"
# NOTE: RX and TX deliberately share ONE characteristic UUID — the official
# Bitchat GATT service exposes a single characteristic that supports both
# write and notify (see AppConstants.Mesh.Gatt in the Android app). Do not
# "fix" this by splitting them.
BITCHAT_RX_CHAR_UUID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"  # host -> peer
BITCHAT_TX_CHAR_UUID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"  # peer -> host

# --- Protocol constants (match BinaryProtocol.kt / BinaryProtocol.swift) ---
PACKET_VERSION = 0x01
PACKET_TYPE_ANNOUNCE = 0x01
PACKET_TYPE_MESSAGE = 0x02
# Fragmentation packet types. The apps split payloads > 512 bytes into
# FRAGMENT (0x20) packets (older builds used START/CONTINUE/END 0x05-0x07).
# The bridge does not reassemble; these are logged and dropped by the link.
PACKET_TYPE_FRAGMENT = 0x20
LEGACY_FRAGMENT_TYPES = (0x05, 0x06, 0x07)
FRAGMENT_TYPES = frozenset((PACKET_TYPE_FRAGMENT, *LEGACY_FRAGMENT_TYPES))
PACKET_TTL = 0x07
FLAG_HAS_RECIPIENT = 0x01
FLAG_HAS_SIGNATURE = 0x02
FLAG_IS_COMPRESSED = 0x04
CANONICAL_TTL_FOR_SIGNING = 0x00  # fixed for relay compatibility
HEADER_SIZE = 14
SIGNATURE_SIZE = 64
SENDER_ID_SIZE = 8

# Privacy padding block sizes (match MessagePadding.kt)
_PADDING_BLOCK_SIZES = (256, 512, 1024, 2048)

# Official apps compress payloads >= 100 bytes (iOS Constants.swift
# compressionThresholdBytes) and verify packet signatures against their own
# re-encoded bytes — i.e. re-compressed with the app's deflate. A packet we
# sign and send UNCOMPRESSED at >= 100 bytes therefore fails signature
# verification on the phone: our signature covers the uncompressed encoding.
# The apps' deflate output is not guaranteed byte-identical to Python zlib's,
# so the only robust policy is to keep every outgoing payload strictly below
# the threshold. Incoming packets are unaffected: we verify against the raw
# transmitted bytes, compressed or not.
SAFE_UNCOMPRESSED_PAYLOAD = 99

# ANNOUNCE payload TLV tags
TLV_NICKNAME = 0x01
TLV_NOISE_KEY = 0x02  # X25519 public key
TLV_SIGNING_KEY = 0x03  # Ed25519 public key


class PacketError(ValueError):
    """Raised when a packet cannot be parsed."""


# Upper bound on inflated payload size (deflate-bomb guard). BLE frames are
# limited to ~64 KiB anyway; the apps enforce their own per-type caps.
MAX_INFLATED_SIZE = 65536


def deflate(data: bytes) -> bytes:
    """Raw DEFLATE (no zlib header), matching the official apps.

    iOS: ``compression_encode_buffer(..., COMPRESSION_ZLIB)``; Android:
    ``Deflater(..., nowrap = true)``. Both emit headerless deflate streams.
    """
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(bytes(data)) + compressor.flush()


def _inflate(stream: bytes, expected_size: int) -> bytes:
    """Inflate a raw-DEFLATE stream, requiring exactly ``expected_size`` bytes.

    A zlib-wrapped stream (RFC 1950 header) is accepted as a fallback, mirroring
    the Android decoder's tolerance. Anything that ends early, over-expands, or
    leaves unconsumed input is rejected.
    """
    last_error: Exception = zlib.error("inflate failed")
    for wbits in (-15, 15):  # raw deflate first, zlib-wrapped fallback
        dec = zlib.decompressobj(wbits)
        try:
            out = dec.decompress(stream, expected_size)
        except zlib.error as exc:
            last_error = exc
            continue
        if len(out) == expected_size and dec.eof and not dec.unconsumed_tail:
            return out
        last_error = zlib.error(
            f"size/completion mismatch (got {len(out)}, want {expected_size})"
        )
    raise last_error


@dataclass
class ParsedPacket:
    packet_type: int
    sender_id: bytes
    timestamp_ms: int
    flags: int
    recipient_id: Optional[bytes]
    payload: bytes  # raw payload (still compressed if flags say so)
    text: str  # decompressed / decoded payload text
    compressed: bool
    signature: Optional[bytes] = None  # raw 64-byte signature, if FLAG_HAS_SIGNATURE

    @property
    def is_broadcast(self) -> bool:
        return self.recipient_id is None or all(b == 0xFF for b in self.recipient_id)


def pad_data(data: bytes) -> bytes:
    """PKCS-style padding to the privacy block sizes used by the app."""
    target = len(data)
    for size in _PADDING_BLOCK_SIZES:
        if len(data) + 16 <= size:
            target = size
            break
    if len(data) >= target:
        return bytes(data)
    needed = target - len(data)
    if needed > 255:  # matches Kotlin: "if (paddingNeeded > 255) return data"
        return bytes(data)
    return bytes(data) + bytes([needed]) * needed


def build_packet(
    identity,
    packet_type: int,
    payload: bytes,
    recipient_id: Optional[bytes] = None,
    compress: bool = False,
    timestamp_ms: Optional[int] = None,
) -> bytes:
    """Build a signed, padded bitchat packet.

    ``identity`` is a :class:`mcbridge.identity.BridgeIdentity`.
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    flags = FLAG_HAS_SIGNATURE
    if recipient_id is not None:
        flags |= FLAG_HAS_RECIPIENT

    body = bytes(payload)
    if compress:
        # 2-byte big-endian original size prefix + raw deflate stream, exactly
        # like the official apps (the Len field counts the prefix too)
        body = len(payload).to_bytes(2, "big") + deflate(payload)
        flags |= FLAG_IS_COMPRESSED

    header = bytearray(HEADER_SIZE)
    header[0] = PACKET_VERSION
    header[1] = packet_type
    header[2] = PACKET_TTL
    struct.pack_into(">Q", header, 3, timestamp_ms)
    header[11] = flags
    struct.pack_into(">H", header, 12, len(body))

    # --- signature over padded unsigned packet, TTL=0, no HAS_SIGNATURE ---
    signing_header = bytearray(header)
    signing_header[2] = CANONICAL_TTL_FOR_SIGNING
    signing_header[11] = flags & ~FLAG_HAS_SIGNATURE

    unsigned = bytearray()
    unsigned += signing_header
    unsigned += identity.my_id
    if recipient_id is not None:
        unsigned += recipient_id[:8]
    unsigned += body
    signature = identity.signing_key.sign(pad_data(bytes(unsigned))).signature

    final = bytearray()
    final += header
    final += identity.my_id
    if recipient_id is not None:
        final += recipient_id[:8]
    final += body
    final += signature
    return pad_data(bytes(final))


def parse_packet(data: bytes) -> ParsedPacket:
    """Parse a raw notification/write payload into a :class:`ParsedPacket`.

    Raises :class:`PacketError` on malformed input, including frames that do
    not actually contain the declared payload (and signature) bytes. Signature
    *verification* is not performed here; callers verify via
    :func:`verify_signature` against a trusted per-peer key.
    """
    if len(data) < HEADER_SIZE:
        raise PacketError(f"packet too short ({len(data)} bytes)")
    version = data[0]
    if version == 0x02:
        raise PacketError(
            "packet version 2 not supported (source routing / large payloads)"
        )
    if version != PACKET_VERSION:
        raise PacketError(f"unsupported packet version {version}")
    packet_type = data[1]
    timestamp_ms = struct.unpack_from(">Q", data, 3)[0]
    flags = data[11]
    payload_len = struct.unpack_from(">H", data, 12)[0]

    offset = HEADER_SIZE
    if len(data) < offset + SENDER_ID_SIZE:
        raise PacketError("truncated sender id")
    sender_id = bytes(data[offset : offset + SENDER_ID_SIZE])
    offset += SENDER_ID_SIZE

    recipient_id = None
    if flags & FLAG_HAS_RECIPIENT:
        if len(data) < offset + SENDER_ID_SIZE:
            raise PacketError("truncated recipient id")
        recipient_id = bytes(data[offset : offset + SENDER_ID_SIZE])
        offset += SENDER_ID_SIZE

    # the frame must actually contain everything it declares, otherwise a
    # corrupted BLE frame would silently decode as a short "valid" message
    declared = payload_len + (SIGNATURE_SIZE if flags & FLAG_HAS_SIGNATURE else 0)
    if len(data) < offset + declared:
        raise PacketError(
            f"truncated frame: declares {payload_len} payload bytes"
            + (" + 64 signature bytes" if flags & FLAG_HAS_SIGNATURE else "")
            + f" but only {len(data) - offset} remain"
        )

    raw = bytes(data[offset : offset + payload_len])
    signature = None
    if flags & FLAG_HAS_SIGNATURE:
        signature = bytes(data[offset + payload_len : offset + payload_len + SIGNATURE_SIZE])

    compressed = bool(flags & FLAG_IS_COMPRESSED)
    if compressed:
        try:
            if len(raw) < 2:
                raise PacketError("compressed payload truncated")
            # 2-byte big-endian original size prefix (official app format)
            claimed = int.from_bytes(raw[:2], "big")
            if not 0 < claimed <= MAX_INFLATED_SIZE:
                raise PacketError(
                    f"compressed payload declares invalid size {claimed}"
                )
            try:
                raw = _inflate(raw[2:], claimed)
            except zlib.error as exc:
                raise PacketError(f"inflate failed: {exc}") from exc
        except PacketError:
            raise

    text = raw.decode("utf-8", errors="ignore")
    return ParsedPacket(
        packet_type=packet_type,
        sender_id=sender_id,
        timestamp_ms=timestamp_ms,
        flags=flags,
        recipient_id=recipient_id,
        payload=bytes(data[offset : offset + payload_len]),
        text=text,
        compressed=compressed,
        signature=signature,
    )


def signing_content(packet: ParsedPacket) -> bytes:
    """Reconstruct the padded, signed byte string for ``packet``.

    Matches :func:`build_packet`: the signature covers the *raw* (still
    compressed) payload, the header with TTL forced to 0 and the
    HAS_SIGNATURE flag cleared, the sender id, the optional recipient id,
    then PKCS-style privacy padding.
    """
    header = bytearray(HEADER_SIZE)
    header[0] = PACKET_VERSION
    header[1] = packet.packet_type
    header[2] = CANONICAL_TTL_FOR_SIGNING
    struct.pack_into(">Q", header, 3, packet.timestamp_ms)
    header[11] = packet.flags & ~FLAG_HAS_SIGNATURE
    struct.pack_into(">H", header, 12, len(packet.payload))

    unsigned = bytearray()
    unsigned += header
    unsigned += packet.sender_id
    if packet.recipient_id is not None:
        unsigned += packet.recipient_id
    unsigned += packet.payload
    return pad_data(bytes(unsigned))


def verify_signature(packet: ParsedPacket, key: bytes) -> bool:
    """Verify ``packet``'s signature against an Ed25519 public ``key``.

    Returns False on any mismatch (or if the packet carries no signature).
    """
    if packet.signature is None or len(key) != 32:
        return False
    try:
        VerifyKey(bytes(key)).verify(signing_content(packet), packet.signature)
    except BadSignatureError:
        return False
    return True


def build_announce_payload(identity, nickname: str) -> bytes:
    """TLV-encoded ANNOUNCE payload: nickname, X25519 pubkey, Ed25519 pubkey."""
    payload = bytearray()
    for tag, value in (
        (TLV_NICKNAME, nickname.encode("utf-8")),
        (TLV_NOISE_KEY, identity.x25519_public),
        (TLV_SIGNING_KEY, identity.public_key_bytes),
    ):
        if len(value) > 255:
            raise ValueError("TLV value too long")
        payload.append(tag)
        payload.append(len(value))
        payload += value
    return bytes(payload)


def parse_announce_tlvs(payload: bytes) -> dict:
    """Parse a TLV blob into ``{tag: bytes}``; tolerates trailing junk."""
    out: dict = {}
    i = 0
    while i + 2 <= len(payload):
        tag = payload[i]
        length = payload[i + 1]
        i += 2
        if i + length > len(payload):
            break
        out[tag] = bytes(payload[i : i + length])
        i += length
    return out
