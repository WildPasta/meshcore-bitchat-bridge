"""Offline tests for the Bitchat packet codec (no hardware needed)."""

import struct
import zlib

import pytest

from mcbridge import bitchat_packets as bp
from mcbridge.identity import BridgeIdentity


@pytest.fixture()
def identity():
    return BridgeIdentity.generate()


def test_message_roundtrip(identity):
    text = "hello from the mesh!"
    packet = bp.build_packet(identity, bp.PACKET_TYPE_MESSAGE, text.encode("utf-8"))
    parsed = bp.parse_packet(packet)

    assert parsed.packet_type == bp.PACKET_TYPE_MESSAGE
    assert parsed.text == text
    assert parsed.sender_id == identity.my_id
    assert parsed.flags & bp.FLAG_HAS_SIGNATURE
    assert parsed.compressed is False
    assert parsed.is_broadcast  # no recipient -> broadcast


def test_padding_block_sizes(identity):
    packet = bp.build_packet(identity, bp.PACKET_TYPE_MESSAGE, b"short message")
    assert len(packet) in bp._PADDING_BLOCK_SIZES
    assert len(packet) == 256

    long_text = "x" * 400
    packet = bp.build_packet(identity, bp.PACKET_TYPE_MESSAGE, long_text.encode())
    parsed = bp.parse_packet(packet)
    assert parsed.text == long_text


def test_packet_with_recipient(identity):
    recipient = b"\xff" * 8
    packet = bp.build_packet(
        identity, bp.PACKET_TYPE_ANNOUNCE, b"payload", recipient_id=recipient
    )
    parsed = bp.parse_packet(packet)

    assert parsed.recipient_id == recipient
    assert parsed.flags & bp.FLAG_HAS_RECIPIENT
    assert parsed.is_broadcast  # ff...ff is the broadcast recipient


def _app_deflate(data: bytes) -> bytes:
    """Compress exactly like the official apps (raw DEFLATE, no header)."""
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(data) + compressor.flush()


def test_compressed_roundtrip(identity):
    text = "this message will be compressed " * 5
    packet = bp.build_packet(
        identity, bp.PACKET_TYPE_MESSAGE, text.encode("utf-8"), compress=True
    )
    parsed = bp.parse_packet(packet)

    assert parsed.compressed is True
    assert parsed.text == text


def test_parse_compressed_app_format(identity):
    # Simulate an app-built compressed payload, mirroring the official
    # BinaryProtocol encoders: 2-byte big-endian original size prefix +
    # raw DEFLATE stream (iOS COMPRESSION_ZLIB / Android Deflater nowrap).
    text = "app style compressed packet that is long enough to be worth compressing " * 2
    comp = len(text.encode()).to_bytes(2, "big") + _app_deflate(text.encode())
    header_flags = bp.FLAG_HAS_SIGNATURE | bp.FLAG_IS_COMPRESSED

    inner = bytearray()
    inner += bytes([bp.PACKET_VERSION, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])
    inner += struct.pack(">Q", 1700000000000)
    inner += bytes([header_flags])
    inner += struct.pack(">H", len(comp))
    inner += identity.my_id
    inner += comp
    inner += b"\x00" * 96  # trailing padding noise (incl. signature bytes)

    parsed = bp.parse_packet(bytes(inner))
    assert parsed.text == text
    assert parsed.compressed is True
    assert parsed.signature is not None


def test_parse_compressed_app_format_zlib_wrapped(identity):
    # The Android decoder also tolerates zlib-wrapped (RFC 1950) streams;
    # the bridge accepts them as a fallback.
    text = "zlib wrapped stream fallback" * 4
    comp = len(text.encode()).to_bytes(2, "big") + zlib.compress(text.encode())

    inner = bytearray()
    inner += bytes([bp.PACKET_VERSION, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])
    inner += struct.pack(">Q", 1700000000000)
    inner += bytes([bp.FLAG_HAS_SIGNATURE | bp.FLAG_IS_COMPRESSED])
    inner += struct.pack(">H", len(comp))
    inner += identity.my_id
    inner += comp
    inner += b"\x11" * 96  # 64 signature bytes + padding noise

    parsed = bp.parse_packet(bytes(inner))
    assert parsed.text == text
    assert parsed.compressed is True


def test_parse_rejects_truncated_payload(identity):
    # header claims 50 payload bytes, frame only carries 3
    inner = bytearray()
    inner += bytes([bp.PACKET_VERSION, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])
    inner += struct.pack(">Q", 1700000000000)
    inner += bytes([0])
    inner += struct.pack(">H", 50)
    inner += identity.my_id
    inner += b"hel"
    with pytest.raises(bp.PacketError, match="truncated frame"):
        bp.parse_packet(bytes(inner))


def test_parse_rejects_declared_but_absent_signature(identity):
    # FLAG_HAS_SIGNATURE set but the 64 signature bytes are missing
    inner = bytearray()
    inner += bytes([bp.PACKET_VERSION, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])
    inner += struct.pack(">Q", 1700000000000)
    inner += bytes([bp.FLAG_HAS_SIGNATURE])
    inner += struct.pack(">H", 3)
    inner += identity.my_id
    inner += b"hey"
    with pytest.raises(bp.PacketError, match="truncated frame"):
        bp.parse_packet(bytes(inner))


def test_signature_roundtrip_and_forgery(identity):
    other = BridgeIdentity.generate()
    packet = bp.build_packet(identity, bp.PACKET_TYPE_MESSAGE, b"signed body")
    parsed = bp.parse_packet(packet)
    assert parsed.signature is not None
    assert bp.verify_signature(parsed, identity.public_key_bytes)
    # right frame, wrong key -> fail
    assert not bp.verify_signature(parsed, other.public_key_bytes)
    # flip a byte in the signature -> fail
    corrupted = bytearray(parsed.signature)
    corrupted[0] ^= 0xFF
    parsed.signature = bytes(corrupted)
    assert not bp.verify_signature(parsed, identity.public_key_bytes)


def test_parse_compressed_rejects_wrong_declared_size(identity):
    text = "size prefix says 200 bytes but the stream expands to less"
    stream = _app_deflate(text.encode())
    comp = (200).to_bytes(2, "big") + stream

    inner = bytearray()
    inner += bytes([bp.PACKET_VERSION, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])
    inner += struct.pack(">Q", 1700000000000)
    inner += bytes([bp.FLAG_HAS_SIGNATURE | bp.FLAG_IS_COMPRESSED])
    inner += struct.pack(">H", len(comp))
    inner += identity.my_id
    inner += comp

    with pytest.raises(bp.PacketError):
        bp.parse_packet(bytes(inner))


def test_parse_compressed_rejects_garbage_stream(identity):
    comp = (64).to_bytes(2, "big") + b"\xde\xad\xbe\xef" * 8

    inner = bytearray()
    inner += bytes([bp.PACKET_VERSION, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])
    inner += struct.pack(">Q", 1700000000000)
    inner += bytes([bp.FLAG_HAS_SIGNATURE | bp.FLAG_IS_COMPRESSED])
    inner += struct.pack(">H", len(comp))
    inner += identity.my_id
    inner += comp

    with pytest.raises(bp.PacketError):
        bp.parse_packet(bytes(inner))


def test_parse_rejects_v2_packets(identity):
    inner = bytearray()
    inner += bytes([0x02, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])  # version 2
    inner += struct.pack(">Q", 1700000000000)
    inner += bytes([bp.FLAG_HAS_SIGNATURE])
    inner += struct.pack(">H", 0)  # v2 uses a 4-byte length; must not parse as v1
    inner += identity.my_id

    with pytest.raises(bp.PacketError, match="version 2"):
        bp.parse_packet(bytes(inner))


def test_parse_rejects_unknown_version(identity):
    inner = bytearray()
    inner += bytes([0x07, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])  # unknown version
    inner += struct.pack(">Q", 1700000000000)
    inner += bytes([bp.FLAG_HAS_SIGNATURE])
    inner += struct.pack(">H", 0)
    inner += identity.my_id

    with pytest.raises(bp.PacketError, match="unsupported packet version"):
        bp.parse_packet(bytes(inner))


def test_announce_tlvs_roundtrip(identity):
    payload = bp.build_announce_payload(identity, "TestBridge")
    tlvs = bp.parse_announce_tlvs(payload)

    assert tlvs[bp.TLV_NICKNAME] == b"TestBridge"
    assert tlvs[bp.TLV_NOISE_KEY] == identity.x25519_public
    assert tlvs[bp.TLV_SIGNING_KEY] == identity.public_key_bytes


def test_announce_via_packet_roundtrip(identity):
    payload = bp.build_announce_payload(identity, "TestBridge")
    packet = bp.build_packet(
        identity, bp.PACKET_TYPE_ANNOUNCE, payload, recipient_id=b"\xff" * 8
    )
    parsed = bp.parse_packet(packet)
    tlvs = bp.parse_announce_tlvs(parsed.payload)
    assert tlvs[bp.TLV_NICKNAME] == b"TestBridge"


def test_malformed_packets_raise(identity):
    with pytest.raises(bp.PacketError):
        bp.parse_packet(b"\x01\x02")
    with pytest.raises(bp.PacketError):
        bp.parse_packet(b"")

    # header claims a payload but packet is truncated mid-sender-id
    truncated = (
        bytes([bp.PACKET_VERSION, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])
        + struct.pack(">Q", 12345)
        + bytes([bp.FLAG_HAS_SIGNATURE])
        + struct.pack(">H", 50)
        + b"\x00\x00"
    )
    with pytest.raises(bp.PacketError):
        bp.parse_packet(truncated)


def test_timestamp_is_embedded(identity):
    ts = 1699999999999
    packet = bp.build_packet(
        identity, bp.PACKET_TYPE_MESSAGE, b"ts", timestamp_ms=ts
    )
    parsed = bp.parse_packet(packet)
    assert parsed.timestamp_ms == ts


def test_identity_determinism():
    a = BridgeIdentity.generate()
    seed = a.signing_key.encode()
    x = a.x25519_private
    # reconstructing from the same seeds must yield the same ids
    import nacl.signing

    b = BridgeIdentity(nacl.signing.SigningKey(seed), x)
    assert a.my_id == b.my_id
    assert a.x25519_public == b.x25519_public
