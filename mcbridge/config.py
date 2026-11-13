"""Configuration: defaults < TOML file < CLI arguments."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

DEFAULT_STATE_DIR = Path.home() / ".config" / "meshcore-bitchat-bridge"
DEFAULT_HASHTAG = "bitchat"
DEFAULT_NICKNAME = "MeshCoreBridge"
DEFAULT_SERIAL_PORTS: Tuple[str, ...] = ("/dev/ttyUSB0", "/dev/ttyACM0")
DEFAULT_LOG_KEEP_DAYS = 14
# accepted log_level names (str.lower'd before lookup); --debug is an alias
LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

logger = logging.getLogger("mcbridge.config")


@dataclass
class Config:
    port: Optional[str] = None
    hashtag: str = DEFAULT_HASHTAG
    nickname: str = DEFAULT_NICKNAME
    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR)
    debug: bool = False
    log_file: Optional[Path] = None
    log_keep_days: int = DEFAULT_LOG_KEEP_DAYS
    # resolved logging.DEBUG..CRITICAL; None = fall back to debug flag/INFO
    log_level: Optional[int] = None

    @property
    def hashtag_name(self) -> str:
        tag = self.hashtag.lstrip("#").strip()
        return f"#{tag or DEFAULT_HASHTAG}"

    def candidate_ports(self) -> list:
        if self.port:
            return [self.port]
        return list(DEFAULT_SERIAL_PORTS)


def _load_toml(path: Path) -> dict:
    if not tomllib:
        logger.warning("tomllib unavailable (Python < 3.11); ignoring config file")
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Could not parse %s: %s", path, exc)
        return {}


def load_config(argv=None) -> Config:
    parser = argparse.ArgumentParser(
        prog="meshcore-bitchat-bridge",
        description="Bridge Bitchat (BLE) <-> MeshCore (LoRa companion node)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML config file (default: <state-dir>/config.toml)",
    )
    parser.add_argument(
        "--port",
        help="serial port of the MeshCore companion node "
        "(default: try %s)" % ", ".join(DEFAULT_SERIAL_PORTS),
    )
    parser.add_argument(
        "--hashtag",
        help=f"MeshCore hashtag channel to bridge, without '#' "
        f"(default: {DEFAULT_HASHTAG})",
    )
    parser.add_argument(
        "--nickname",
        help=f"Bitchat nickname for the bridge (default: {DEFAULT_NICKNAME})",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help=f"directory for the persistent identity/config; also the "
        f"default location of config.toml unless --config is given "
        f"(default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="log file path (default: <state-dir>/logs/bridge.log)",
    )
    parser.add_argument(
        "--log-keep-days",
        type=int,
        help=f"days of daily-rotated logs to keep "
        f"(default: {DEFAULT_LOG_KEEP_DAYS}, minimum 1)",
    )
    parser.add_argument(
        "--log-level",
        type=str.lower,
        choices=sorted(LOG_LEVELS),
        default=None,
        help="log level: debug, info, warning, error or critical "
        "(default: info; --debug is an alias for --log-level debug)",
    )
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args(argv)

    # Resolve the config path: explicit --config wins; otherwise it lives in
    # the state dir, so --state-dir relocates the default location.
    if args.config is not None:
        config_path = args.config.expanduser()
    elif args.state_dir is not None:
        config_path = Path(args.state_dir).expanduser() / "config.toml"
    else:
        config_path = DEFAULT_STATE_DIR / "config.toml"

    data = _load_toml(config_path)
    if not isinstance(data, dict):
        data = {}

    def _toml_str(key: str) -> Optional[str]:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            logger.warning("Ignoring invalid %r in %s", key, config_path)
            return None
        return value

    debug_value = data.get("debug", False)
    if not isinstance(debug_value, bool):
        logger.warning("Ignoring non-boolean 'debug' in %s", config_path)
        debug_value = False

    def _toml_int(key: str, default: int) -> int:
        value = data.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            logger.warning("Ignoring invalid %r in %s", key, config_path)
            return default
        return value

    log_keep_days = (
        args.log_keep_days
        if args.log_keep_days is not None
        else _toml_int("log_keep_days", DEFAULT_LOG_KEEP_DAYS)
    )
    if log_keep_days < 1:
        logger.warning(
            "log_keep_days must be >= 1 (got %d); clamping to 1", log_keep_days
        )
        log_keep_days = 1

    log_file_value = args.log_file or _toml_str("log_file")

    # log level: CLI (validated by argparse choices) > TOML > debug flag
    log_level_name = args.log_level
    if log_level_name is None:
        toml_level = _toml_str("log_level")
        if toml_level is not None:
            normalized = toml_level.strip().lower()
            if normalized in LOG_LEVELS:
                log_level_name = normalized
            else:
                logger.warning(
                    "Ignoring invalid 'log_level' %r in %s "
                    "(expected one of: %s)",
                    toml_level,
                    config_path,
                    ", ".join(sorted(LOG_LEVELS)),
                )

    return Config(
        port=args.port or _toml_str("port"),
        hashtag=(args.hashtag or _toml_str("hashtag") or DEFAULT_HASHTAG),
        nickname=(args.nickname or _toml_str("nickname") or DEFAULT_NICKNAME),
        state_dir=Path(
            args.state_dir or _toml_str("state_dir") or DEFAULT_STATE_DIR
        ).expanduser(),
        debug=bool(args.debug or debug_value),
        log_file=(
            Path(str(log_file_value)).expanduser() if log_file_value else None
        ),
        log_keep_days=log_keep_days,
        log_level=LOG_LEVELS[log_level_name] if log_level_name else None,
    )
