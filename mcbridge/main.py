"""Main entrypoint: config, wiring, supervision, shutdown."""

from __future__ import annotations

import asyncio
import logging
import signal

from . import __version__
from .bitchat_link import BitchatLink
from .config import load_config
from .identity import load_or_create
from .logging_setup import effective_level, setup_logging
from .meshcore_link import MeshCoreLink
from .router import MessageRateLimiter, Router

logger = logging.getLogger("mcbridge")

_CHUNK_PAUSE = 0.5


async def main() -> None:
    cfg = load_config()

    log_path = setup_logging(cfg)
    if log_path is not None:
        logger.info(
            "Logging to %s (keeping %d days)", log_path, cfg.log_keep_days
        )
    # the meshcore lib's DEBUG is very chatty; cap it at INFO unless the user explicitly requested DEBUG
    if effective_level(cfg) > logging.DEBUG:
        logging.getLogger("meshcore").setLevel(logging.INFO)

    logger.info("meshcore-bitchat-bridge v%s starting", __version__)
    logger.info(
        "Config: channel=%s nickname=%r state_dir=%s",
        cfg.hashtag_name,
        cfg.nickname,
        cfg.state_dir,
    )

    identity = load_or_create(cfg.state_dir)
    router = Router()
    rate_limiter = MessageRateLimiter()

    stop_event = asyncio.Event()

    # ------------------------------------------------------------ wiring
    async def on_bitchat_message(nickname, sender_hex, text):
        if not rate_limiter.allow(sender_hex):
            logger.info(
                "Rate limit: dropping message from %s (burst of %d exceeded)",
                nickname or sender_hex[-4:],
                rate_limiter.capacity,
            )
            return
        chunks = router.bitchat_to_mesh(nickname, text)
        for i, chunk in enumerate(chunks):
            if i:
                await asyncio.sleep(_CHUNK_PAUSE)
            logger.info("(Bridge -> LoRa) %s", chunk)
            if not await mesh_link.send(chunk):
                logger.warning(
                    "Failed to send LoRa chunk %d/%d "
                    "(recipients may see a dangling multi-part message)",
                    i + 1,
                    len(chunks),
                )

    async def on_mesh_message(text, timestamp, channel_idx):
        pieces, reason = router.mesh_to_bitchat_with_reason(
            text, timestamp, channel_idx
        )
        if not pieces:
            logger.debug(
                "(LoRa -> Bridge) dropped (%s): %r", reason, text[:60]
            )
            return
        for i, piece in enumerate(pieces):
            if i:
                # small pause between pieces of a long message
                await asyncio.sleep(_CHUNK_PAUSE)
            logger.info("(LoRa -> Bridge) %s", piece)
            await bitchat_link.broadcast(piece)

    mesh_link = MeshCoreLink(
        ports=cfg.candidate_ports(),
        hashtag=cfg.hashtag_name,
        on_message=on_mesh_message,
    )
    bitchat_link = BitchatLink(
        identity=identity,
        nickname=cfg.nickname,
        on_message=on_bitchat_message,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover (non-Unix)
            pass

    tasks = [
        asyncio.create_task(mesh_link.run(), name="meshcore-link"),
        asyncio.create_task(bitchat_link.run(), name="bitchat-link"),
    ]

    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        await mesh_link.stop()
        await bitchat_link.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Shutdown complete.")


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
