"""Tests for identity persistence: corruption recovery vs hard IO failure."""

import hashlib
import os

import pytest

from mcbridge.identity import BridgeIdentity, IDENTITY_FILE, load_or_create


def test_peer_id_derived_from_noise_key():
    # official apps derive the peer id from the Noise/X25519 static key
    # (WHITEPAPER §3) and reject announces whose id doesn't match it
    ident = BridgeIdentity.generate()
    assert ident.my_id == hashlib.sha256(ident.x25519_public).digest()[:8]


def test_load_roundtrip(tmp_path):
    a = load_or_create(tmp_path)
    b = load_or_create(tmp_path)
    assert a.my_id == b.my_id
    assert os.stat(tmp_path / IDENTITY_FILE).st_mode & 0o777 == 0o600


def test_corrupt_file_is_moved_aside_and_regenerated(tmp_path):
    path = tmp_path / IDENTITY_FILE
    path.write_text("{not json at all")
    original_stat = path.stat()

    identity = load_or_create(tmp_path)  # must not raise

    bad = tmp_path / (IDENTITY_FILE + ".bad")
    assert bad.exists()
    assert bad.read_text() == "{not json at all"
    assert (tmp_path / IDENTITY_FILE).exists()
    # regenerated file parses and differs from the corrupt original mtime
    assert identity.my_id == load_or_create(tmp_path).my_id
    assert original_stat.st_mtime == bad.stat().st_mtime


def test_invalid_key_lengths_are_corruption_too(tmp_path):
    (tmp_path / IDENTITY_FILE).write_text(
        '{"ed25519_seed": "abcd", "x25519_private": "abcd"}'
    )
    identity = load_or_create(tmp_path)
    assert isinstance(identity, BridgeIdentity)
    assert (tmp_path / (IDENTITY_FILE + ".bad")).exists()


def test_unreadable_file_raises(tmp_path):
    # a directory at the identity path makes read_text raise IsADirectoryError
    # (an OSError): must surface, not silently regenerate
    (tmp_path / IDENTITY_FILE).mkdir()
    with pytest.raises(RuntimeError, match="cannot be read"):
        load_or_create(tmp_path)
    assert not (tmp_path / (IDENTITY_FILE + ".bad")).exists()
