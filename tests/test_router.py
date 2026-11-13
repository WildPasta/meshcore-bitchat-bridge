"""Tests for the message router (formatting, chunking, loop prevention)."""

from mcbridge.router import (
    Router,
    BITCHAT_ORIGIN_PREFIX,
    MessageRateLimiter,
    utf8_chunk,
)


def test_bitchat_to_mesh_basic():
    r = Router()
    msgs = r.bitchat_to_mesh("Alice", "hello mesh")
    assert msgs == [f"{BITCHAT_ORIGIN_PREFIX}Alice: hello mesh"]


def test_bitchat_to_mesh_missing_nick():
    r = Router()
    msgs = r.bitchat_to_mesh(None, "hi")
    assert msgs == [f"{BITCHAT_ORIGIN_PREFIX}peer: hi"]


def test_bitchat_to_mesh_chunking():
    r = Router(max_mesh_text=160)
    long_text = "word " * 120  # 600 chars
    msgs = r.bitchat_to_mesh("Bob", long_text)

    assert len(msgs) > 1
    for i, m in enumerate(msgs, 1):
        assert len(m.encode("utf-8")) <= 160
        assert m.startswith(f"{BITCHAT_ORIGIN_PREFIX}Bob: ({i}/{len(msgs)}) ")


def test_bitchat_to_mesh_collapses_whitespace():
    r = Router()
    msgs = r.bitchat_to_mesh("Alice", "line one\nline two  spaced")
    assert msgs == [f"{BITCHAT_ORIGIN_PREFIX}Alice: line one line two spaced"]


def test_mesh_to_bitchat_with_sender():
    r = Router()
    out = r.mesh_to_bitchat("node-42: I'm on my way", 1700000000)
    assert out == ["[Mesh: node-42] I'm on my way"]


def test_mesh_to_bitchat_without_sender():
    r = Router()
    out = r.mesh_to_bitchat("just a plain message", 1700000000)
    assert out == ["[Mesh] just a plain message"]


def test_mesh_to_bitchat_drops_bridge_traffic():
    r = Router()
    assert r.mesh_to_bitchat(f"{BITCHAT_ORIGIN_PREFIX}Alice: hi", 1) == []


def test_mesh_to_bitchat_dedup():
    r = Router()
    text = "node-1: duplicate me"
    first = r.mesh_to_bitchat(text, 123)
    second = r.mesh_to_bitchat(text, 123)  # flood echo, same ts+text
    assert first == ["[Mesh: node-1] duplicate me"]
    assert second == []

    # different timestamp is treated as a new message
    assert r.mesh_to_bitchat(text, 456) == ["[Mesh: node-1] duplicate me"]


def test_mesh_to_bitchat_dedup_is_channel_scoped():
    r = Router()
    text = "node-1: same text"
    assert r.mesh_to_bitchat(text, 123, channel_idx=1) != []
    # same timestamp+text on a different channel is not a duplicate
    assert r.mesh_to_bitchat(text, 123, channel_idx=2) != []
    # same channel+timestamp+text still is
    assert r.mesh_to_bitchat(text, 123, channel_idx=1) == []


def test_mesh_to_bitchat_dedup_window_expires():
    # dedup is bounded by RECEIVE time: the same (timestamp, text) is relayed
    # again once the window has passed (dead-clock nodes must not be silenced)
    now = [100.0]
    r = Router(clock=lambda: now[0])
    text = "node-1: hello"
    assert r.mesh_to_bitchat(text, 0) != []  # sender_timestamp=0
    assert r.mesh_to_bitchat(text, 0) == []  # within the window
    now[0] += 31.0  # past the 30 s window
    assert r.mesh_to_bitchat(text, 0) != []


def test_mesh_to_bitchat_dedup_caps_history():
    now = [0.0]
    r = Router(clock=lambda: now[0])
    for i in range(300):
        assert r.mesh_to_bitchat(f"node-{i}: msg", i) != []
    assert len(r._mesh_seen) == 256


def test_mesh_to_bitchat_url_is_not_a_sender():
    r = Router()
    assert r.mesh_to_bitchat("https://x.example/page", 1) == [
        "[Mesh] https://x.example/page"
    ]


def test_mesh_to_bitchat_drop_reasons():
    r = Router()
    assert r.mesh_to_bitchat_with_reason("   ", 1) == ([], "empty")
    assert r.mesh_to_bitchat_with_reason("bitchat/Alice: hi", 1) == (
        [],
        "loop-prevention marker",
    )
    assert r.mesh_to_bitchat_with_reason("node-1: hi", 1)[0] != []
    assert r.mesh_to_bitchat_with_reason("node-1: hi", 1) == ([], "duplicate")


def test_mesh_to_bitchat_chunks_under_compression_threshold():
    # official apps compress payloads >= 100 bytes and then fail to verify
    # our signature (signed over the uncompressed encoding) — every BLE
    # broadcast payload must stay strictly below that threshold
    r = Router()
    body = "word " * 60  # 300 chars
    pieces = r.mesh_to_bitchat(f"node-1: {body}", 1)
    assert len(pieces) > 1
    for i, piece in enumerate(pieces, 1):
        assert len(piece.encode("utf-8")) <= 99
        assert piece.startswith(f"[Mesh: node-1] ({i}/{len(pieces)}) ")
    # no content lost (markers and prefixes aside, words all present)
    relayed = " ".join(p.split(") ", 1)[1] for p in pieces)
    assert relayed.replace(" ", "") == body.replace(" ", "")


def test_mesh_to_bitchat_short_message_is_single_piece():
    r = Router()
    pieces = r.mesh_to_bitchat("node-1: hi", 1)
    assert pieces == ["[Mesh: node-1] hi"]
    assert len(pieces[0].encode("utf-8")) <= 99


def test_mesh_to_bitchat_long_unprefixed_text_chunks():
    r = Router()
    pieces = r.mesh_to_bitchat("x" * 300, 1)
    assert len(pieces) > 1
    for i, piece in enumerate(pieces, 1):
        assert len(piece.encode("utf-8")) <= 99
        assert piece.startswith(f"[Mesh] ({i}/{len(pieces)}) ")


def test_bitchat_to_mesh_sanitizes_control_chars():
    r = Router()
    msgs = r.bitchat_to_mesh("a\nFAKE: injected", "hi")
    # \n removed; the remainder is exactly 15 chars so it fits
    assert msgs == [f"{BITCHAT_ORIGIN_PREFIX}aFAKE: injected: hi"]
    assert "\n" not in msgs[0]

    msgs = r.bitchat_to_mesh("bad\x7f\x01nick", "hi")
    assert msgs == [f"{BITCHAT_ORIGIN_PREFIX}badnick: hi"]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_rate_limiter_burst_and_refill():
    clock = FakeClock()
    limiter = MessageRateLimiter(clock=clock)
    sender = "a" * 16
    assert limiter.allow(sender)
    assert limiter.allow(sender)
    assert limiter.allow(sender)
    assert not limiter.allow(sender)  # 4th in a burst is dropped
    # other senders are unaffected
    assert limiter.allow("b" * 16)
    # after a full minute the bucket is refilled to capacity again
    clock.now += 60.0
    assert limiter.allow(sender)


def test_bitchat_to_mesh_truncates_oversized_text():
    r = Router()
    msgs = r.bitchat_to_mesh("Alice", "x" * 5000)
    joined = " ".join(m.split(": ", 1)[1] for m in msgs)
    assert "[truncated]" in msgs[-1]
    # roughly capped: 1000 bytes of text + markers + chunk headers
    assert len(joined.encode("utf-8")) < 1400


def test_bitchat_to_mesh_caps_nickname():
    r = Router()
    long_nick = "n" * 40
    msgs = r.bitchat_to_mesh(long_nick, "hi")
    assert msgs == [f"{BITCHAT_ORIGIN_PREFIX}{'n' * 15}: hi"]
    # multi-byte nickname must be cut on a character boundary
    emoji_nick = "é" * 20  # 40 bytes utf-8
    msgs = r.bitchat_to_mesh(emoji_nick, "hi")
    assert msgs == [f"{BITCHAT_ORIGIN_PREFIX}{'é' * 15}: hi"]


def test_utf8_chunk_respects_byte_budget():
    text = "é" * 100  # 2 bytes per char
    chunks = utf8_chunk(text, 20)
    for c in chunks:
        assert len(c.encode("utf-8")) <= 20
    assert "".join(chunks) == text


def test_utf8_chunk_long_word_split():
    text = "a" * 100
    chunks = utf8_chunk(text, 20)
    assert all(len(c) <= 20 for c in chunks)
    assert "".join(chunks) == text
