"""Persistent Bitchat identity for the bridge.

Keeps a stable Ed25519 signing key and X25519 key across restarts, so phones
see the same bridge peer every time. The reference meshtastic bridge
regenerated keys per run, which caused stale peer state on phones.

Peer id derivation: SHA256(X25519 public key)[:8] — the *Noise* key, not the
Ed25519 signing key. Official apps derive the id this way (WHITEPAPER §3:
"first 8 bytes of the SHA-256 fingerprint of the device's Noise static key")
and enforce it on every ANNOUNCE (iOS `BLEAnnouncePreflightPolicy` rejects
announces whose sender id doesn't match the announced noise key). Deriving
the id from the signing key instead gets every ANNOUNCE rejected as
senderMismatch and all bridge traffic silently dropped by phones.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import nacl.bindings
import nacl.encoding
import nacl.signing

logger = logging.getLogger("mcbridge.identity")

IDENTITY_FILE = "identity.json"


class BridgeIdentity:
    """Bitchat identity keys."""

    def __init__(self, signing_key: nacl.signing.SigningKey, x25519_private: bytes):
        self.signing_key = signing_key
        self.public_key_bytes = signing_key.verify_key.encode(
            encoder=nacl.encoding.RawEncoder
        )
        self.x25519_private = x25519_private
        self.x25519_public = nacl.bindings.crypto_scalarmult_base(x25519_private)
        # Id from the Noise (X25519) key — see module docstring. The Ed25519
        # key above is only used to sign packets.
        self.my_id = hashlib.sha256(self.x25519_public).digest()[:8]

    @property
    def short_id(self) -> str:
        return self.my_id.hex()[-4:]

    @classmethod
    def generate(cls) -> "BridgeIdentity":
        signing_key = nacl.signing.SigningKey.generate()
        _, x25519_private = nacl.bindings.crypto_box_keypair()
        return cls(signing_key, x25519_private)


def load_or_create(state_dir: Path) -> BridgeIdentity:
    """Load the bridge identity from ``state_dir`` or create + persist it."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / IDENTITY_FILE

    if path.exists():
        try:
            raw = path.read_text()
        except OSError as exc:
            # an OS/IO error (permissions, EIO, ...) must not silently
            # regenerate the identity — phones would see a new peer
            raise RuntimeError(
                f"identity file {path} exists but cannot be read: {exc}"
            ) from exc
        try:
            data = json.loads(raw)
            seed = bytes.fromhex(data["ed25519_seed"])
            x25519_private = bytes.fromhex(data["x25519_private"])
            if len(seed) != 32 or len(x25519_private) != 32:
                raise ValueError("key fields must be 32 bytes")
            identity = BridgeIdentity(
                nacl.signing.SigningKey(seed), x25519_private
            )
            logger.info("Loaded bridge identity (id %s)", identity.my_id.hex())
            return identity
        except Exception as exc:
            # file exists but is corrupt: preserve it for forensics, regenerate
            bad = state_dir / (IDENTITY_FILE + ".bad")
            try:
                os.replace(path, bad)
                logger.warning(
                    "Corrupt identity file %s (%s); moved to %s and regenerating",
                    path,
                    exc,
                    bad,
                )
            except OSError:
                logger.warning(
                    "Corrupt identity file %s (%s); could not move it aside; "
                    "regenerating anyway",
                    path,
                    exc,
                )

    identity = BridgeIdentity.generate()
    data = (
        json.dumps(
            {
                "ed25519_seed": identity.signing_key.encode(
                    encoder=nacl.encoding.RawEncoder
                ).hex(),
                "x25519_private": identity.x25519_private.hex(),
            },
            indent=2,
        )
        + "\n"
    )
    # Create/write with 0600 from the start so there is no world-readable
    # window between creation and chmod; the trailing chmod also tightens a
    # pre-existing (e.g. corrupt) file that is being replaced.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.chmod(path, 0o600)
    logger.info("Created new bridge identity (id %s) at %s", identity.my_id.hex(), path)
    return identity
