"""Offline tests for BitchatLink packet handling (TOFA verification, gates).

No BLE hardware is touched — packets are fed directly to _process_packet.
"""

import asyncio
import logging
import struct

import pytest

from mcbridge import bitchat_packets as bp
from mcbridge.bitchat_link import BitchatLink
from mcbridge.identity import BridgeIdentity

BROADCAST = b"\xff" * 8
DM_RECIPIENT = b"\x01\x02\x03\x04\x05\x06\x07\x08"


@pytest.fixture(autouse=True)
def fast_debounce(monkeypatch):
    # the nickname-log debounce sleeps in real time; make tests instant
    monkeypatch.setattr("mcbridge.bitchat_link._NICKNAME_DEBOUNCE", 0.0)


def make_link():
    bridge_ident = BridgeIdentity.generate()
    received = []

    async def on_message(nickname, sender_hex, text):
        received.append((nickname, sender_hex, text))

    return bridge_ident, BitchatLink(bridge_ident, "Bridge", on_message), received


def make_peer(nickname="peer"):
    ident = BridgeIdentity.generate()
    announce = bp.build_packet(
        ident,
        bp.PACKET_TYPE_ANNOUNCE,
        bp.build_announce_payload(ident, nickname),
        recipient_id=BROADCAST,
    )
    return ident, announce


def rewire_sender(frame: bytes, new_sender_id: bytes) -> bytes:
    """Point a built frame's sender-id field at another peer (spoof helper)."""
    raw = bytearray(frame)
    raw[bp.HEADER_SIZE : bp.HEADER_SIZE + bp.SENDER_ID_SIZE] = new_sender_id
    return bytes(raw)


def process(link: BitchatLink, data: bytes):
    """Run _process_packet, letting spawned nickname tasks settle."""

    async def run():
        await link._process_packet(data, "aa:bb:cc:dd:ee:ff")
        pending = [t for t in list(link._nickname_tasks.values()) if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(run())


def announce_peer(link: BitchatLink, ident: BridgeIdentity, nickname="peer"):
    announce = bp.build_packet(
        ident,
        bp.PACKET_TYPE_ANNOUNCE,
        bp.build_announce_payload(ident, nickname),
        recipient_id=BROADCAST,
    )
    process(link, announce)


# --------------------------------------------------------------- broadcast gate
def test_dm_is_not_relayed():
    _, link, received = make_link()
    peer, _ = make_peer()
    announce_peer(link, peer)

    dm = bp.build_packet(
        peer, bp.PACKET_TYPE_MESSAGE, b"secret whisper", recipient_id=DM_RECIPIENT
    )
    process(link, dm)
    assert received == []


def test_broadcast_message_is_relayed():
    _, link, received = make_link()
    peer, _ = make_peer()
    announce_peer(link, peer, "Alice")

    msg = bp.build_packet(
        peer, bp.PACKET_TYPE_MESSAGE, b"hello mesh", recipient_id=BROADCAST
    )
    process(link, msg)
    assert received == [("Alice", peer.my_id.hex(), "hello mesh")]


def test_same_millisecond_announce_and_message_both_processed():
    # regression: dedup used to key on (sender, timestamp) only, so the
    # on-connect ANNOUNCE+MESSAGE sequence sent within one millisecond
    # collapsed and the message was silently dropped
    _, link, received = make_link()
    peer, _ = make_peer()
    ts = 1700000000000

    announce = bp.build_packet(
        peer,
        bp.PACKET_TYPE_ANNOUNCE,
        bp.build_announce_payload(peer, "Alice"),
        recipient_id=BROADCAST,
        timestamp_ms=ts,
    )
    msg = bp.build_packet(
        peer,
        bp.PACKET_TYPE_MESSAGE,
        b"hello mesh",
        recipient_id=BROADCAST,
        timestamp_ms=ts,  # same millisecond, different type
    )
    process(link, announce)
    process(link, msg)
    assert received == [("Alice", peer.my_id.hex(), "hello mesh")]


def test_duplicate_frame_is_still_deduped():
    # an exact echo of the same frame (multi-connection relay) must still
    # be dropped now that the dedup key includes the packet type
    _, link, received = make_link()
    peer, _ = make_peer()
    announce_peer(link, peer)
    msg = bp.build_packet(
        peer, bp.PACKET_TYPE_MESSAGE, b"once", recipient_id=BROADCAST
    )
    process(link, msg)
    process(link, msg)  # relayed echo
    assert received == [("peer", peer.my_id.hex(), "once")]


# --------------------------------------------------- signature verification
def test_signed_message_from_announced_peer_is_relayed():
    _, link, received = make_link()
    peer, _ = make_peer()
    announce_peer(link, peer)

    msg = bp.build_packet(peer, bp.PACKET_TYPE_MESSAGE, b"signed hello")
    assert bp.PACKET_TYPE_MESSAGE == bp.parse_packet(msg).packet_type
    process(link, msg)
    assert len(received) == 1


def test_forged_signature_is_dropped(caplog):
    _, link, received = make_link()
    victim, _ = make_peer()
    announce_peer(link, victim)  # victim's key is now trusted

    forger = BridgeIdentity.generate()
    forged = rewire_sender(
        bp.build_packet(forger, bp.PACKET_TYPE_MESSAGE, b"i am the victim"),
        victim.my_id,
    )
    with caplog.at_level("WARNING", logger="mcbridge.bitchat"):
        process(link, forged)
    assert received == []
    assert any("spoofing" in r.message.lower() for r in caplog.records)
    # key is retained after a bad signature
    assert victim.my_id.hex() in link.peer_keys


def test_unknown_sender_is_dropped(caplog):
    _, link, received = make_link()
    stranger = BridgeIdentity.generate()  # never announced

    msg = bp.build_packet(stranger, bp.PACKET_TYPE_MESSAGE, b"who am i")
    with caplog.at_level("WARNING", logger="mcbridge.bitchat"):
        process(link, msg)
    assert received == []
    assert any("unknown sender" in r.message for r in caplog.records)


def test_unsigned_message_is_dropped():
    _, link, received = make_link()
    peer, _ = make_peer()
    announce_peer(link, peer)

    # build the frame without FLAG_HAS_SIGNATURE: parse is tolerant, the
    # link is not
    inner = bytearray()
    inner += bytes([bp.PACKET_VERSION, bp.PACKET_TYPE_MESSAGE, bp.PACKET_TTL])
    inner += struct.pack(">Q", 1700000000000)
    inner += bytes([0])
    inner += struct.pack(">H", 2)
    inner += peer.my_id
    inner += b"hi"
    process(link, bytes(inner))
    assert received == []


# --------------------------------------------------------- ANNOUNCE handling
def test_announce_with_mismatched_key_hash_is_dropped(caplog):
    _, link, received = make_link()
    claimed = BridgeIdentity.generate()  # id the packet claims
    holder = BridgeIdentity.generate()  # key actually carried

    payload = bytearray(bp.build_announce_payload(claimed, "Impostor"))
    # swap the TLV_NOISE_KEY value for holder's key: the id no longer
    # matches the announced noise key (official apps enforce exactly this)
    tlv = bp.parse_announce_tlvs(bytes(payload))
    old_key = tlv[bp.TLV_NOISE_KEY]
    idx = payload.index(old_key)
    payload[idx : idx + len(old_key)] = holder.x25519_public
    forged_announce = bp.build_packet(
        claimed, bp.PACKET_TYPE_ANNOUNCE, bytes(payload), recipient_id=BROADCAST
    )

    with caplog.at_level("WARNING", logger="mcbridge.bitchat"):
        process(link, forged_announce)
    assert claimed.my_id.hex() not in link.peer_nicknames
    assert claimed.my_id.hex() not in link.peer_keys
    assert holder.my_id.hex() not in link.peer_keys
    assert any("noise key hash" in r.message for r in caplog.records)


def test_announce_without_noise_key_is_dropped():
    _, link, _ = make_link()
    peer = BridgeIdentity.generate()
    # nickname + signing key TLVs only — no TLV_NOISE_KEY
    payload = b"\x01\x04nick" + b"\x03\x20" + peer.public_key_bytes
    bare = bp.build_packet(
        peer, bp.PACKET_TYPE_ANNOUNCE, payload, recipient_id=BROADCAST
    )
    process(link, bare)
    assert peer.my_id.hex() not in link.peer_keys
    assert peer.my_id.hex() not in link.peer_nicknames


def test_announce_stores_key_and_sanitized_nickname():
    _, link, _ = make_link()
    peer, _ = make_peer("Nick\nFAKE: x")
    announce_peer(link, peer, "Nick\nFAKE: x")
    assert link.peer_nicknames[peer.my_id.hex()] == "NickFAKE: x"
    assert link.peer_keys[peer.my_id.hex()] == peer.public_key_bytes


def test_announce_without_signing_key_is_dropped():
    _, link, _ = make_link()
    peer = BridgeIdentity.generate()
    bare = bp.build_packet(
        peer, bp.PACKET_TYPE_ANNOUNCE, b"\x01\x04nick", recipient_id=BROADCAST
    )
    process(link, bare)
    assert peer.my_id.hex() not in link.peer_keys
    assert peer.my_id.hex() not in link.peer_nicknames


# --------------------------------------------------------- resource bounds
def test_peer_maps_are_bounded():
    _, link, _ = make_link()
    for _ in range(600):
        peer = BridgeIdentity.generate()
        announce_peer(link, peer, "x")
    assert len(link.peer_keys) <= 500
    assert len(link.peer_nicknames) <= 500


def test_nickname_tasks_are_evicted_when_done():
    _, link, _ = make_link()
    peer, _ = make_peer()
    announce_peer(link, peer)
    # debounce task ran to completion inside process(); its entry must be gone
    assert peer.my_id.hex() not in link._nickname_tasks


# --------------------------------------------------- disconnect log level
def test_disconnect_logs_info_only_for_established_peers(caplog):
    # the callback is registered pre-connect and once per retry attempt, so
    # bleak fires it for probes that never established; those must not spam
    # INFO (rotating privacy MACs can produce many failed probes per minute)
    _, link, _ = make_link()

    class FakeClient:
        def __init__(self, address):
            self.address = address

    with caplog.at_level("DEBUG", logger="mcbridge.bitchat"):
        # a probe that never connected: DEBUG only
        link._on_client_disconnect(FakeClient("AA:BB:CC:DD:EE:01"))

        # an established peer dropping: INFO, and cleaned up
        established = FakeClient("AA:BB:CC:DD:EE:02")
        link.connected_clients[established.address] = established
        link._on_client_disconnect(established)

    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert [r.getMessage() for r in infos] == [
        "Bitchat peer AA:BB:CC:DD:EE:02 disconnected"
    ]
    assert any(
        r.levelno == logging.DEBUG
        and "AA:BB:CC:DD:EE:01" in r.getMessage()
        and "without connecting" in r.getMessage()
        for r in caplog.records
    )
    assert established.address not in link.connected_clients
