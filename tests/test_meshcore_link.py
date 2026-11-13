"""Tests for MeshCoreLink channel-joining logic using a mock connection."""

import asyncio
import hashlib

import pytest
from meshcore import EventType

from mcbridge.meshcore_link import MeshCoreLink, MeshCoreLinkError


class FakeCommands:
    def __init__(self, slots):
        self.slots = slots  # list of (name, secret bytes) or None for errors
        self.set_calls = []

    async def get_channel(self, idx):
        class R:
            pass

        r = R()
        if idx >= len(self.slots) or self.slots[idx] == "ERR":
            r.type = EventType.ERROR
            r.payload = {"reason": "not found"}
            return r
        name, secret = self.slots[idx]
        r.type = EventType.CHANNEL_INFO
        r.payload = {"channel_idx": idx, "channel_name": name, "channel_secret": secret}
        return r

    async def set_channel(self, idx, name, secret=None):
        self.set_calls.append((idx, name))
        class R:
            pass
        r = R()
        r.type = EventType.OK
        r.payload = {}
        return r


class FakeMeshCore:
    def __init__(self, slots):
        self.commands = FakeCommands(slots)
        self.self_info = {"name": "heltec-test"}
        self.disconnected = False

    def subscribe(self, *a, **kw):
        pass

    async def start_auto_message_fetching(self):
        pass

    async def send_device_query(self):
        class R:
            pass
        r = R()
        r.type = EventType.DEVICE_INFO
        r.payload = {"max_channels": len(self.commands.slots)}
        return r

    async def disconnect(self):
        self.disconnected = True

    @property
    def is_connected(self):
        return not self.disconnected


async def _run_ensure(slots, hashtag="bitchat"):
    link = MeshCoreLink(["/dev/null"], hashtag, on_message=lambda t, ts, idx: None)
    mc = FakeMeshCore(slots)
    idx = await link._ensure_channel(mc, len(slots))
    return link, mc, idx


def test_ensure_channel_finds_existing_slot():
    secret = hashlib.sha256(b"#bitchat").digest()[:16]
    loop = asyncio.new_event_loop()
    link, mc, idx = loop.run_until_complete(
        _run_ensure([("Public", b"\x8b\x33\x87\xe9\xc5\xcd\xea\x6a\xc9\xe5\xed\xba\xa1\x15\xcd\x72"),
                     ("bitchat", secret),
                     ("", b"\x00" * 16)])
    )
    loop.close()
    assert idx == 1
    assert mc.commands.set_calls == []  # reused, not recreated


def test_ensure_channel_creates_in_first_empty_slot():
    loop = asyncio.new_event_loop()
    link, mc, idx = loop.run_until_complete(
        _run_ensure([("Public", b"\x8b" * 16), ("", b"\x00" * 16), ("", b"\x00" * 16)])
    )
    loop.close()
    assert idx == 1  # first empty slot
    assert mc.commands.set_calls == [(1, "#bitchat")]


def test_ensure_channel_no_free_slot():
    loop = asyncio.new_event_loop()
    with pytest.raises(MeshCoreLinkError):
        loop.run_until_complete(
            _run_ensure([("Public", b"\x8b" * 16), ("other", b"\x01" * 16)])
        )
    loop.close()


def test_ensure_channel_skips_error_slots():
    loop = asyncio.new_event_loop()
    link, mc, idx = loop.run_until_complete(
        _run_ensure(["ERR", ("", b"\x00" * 16)])
    )
    loop.close()
    assert idx == 1
    assert mc.commands.set_calls == [(1, "#bitchat")]
