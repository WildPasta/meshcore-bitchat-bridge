"""Tests for mcbridge.logging_setup (offline, tmp_path-based)."""

from __future__ import annotations

import gzip
import logging
import logging.handlers
import re
import time
from io import StringIO

import pytest

from mcbridge.config import Config, load_config
from mcbridge.logging_setup import default_log_path, setup_logging


@pytest.fixture(autouse=True)
def _clean_mcbridge_logger():
    """Detach and close every handler setup_logging added during a test."""
    yield
    root = logging.getLogger("mcbridge")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.NOTSET)


def _make_cfg(tmp_path, **overrides) -> Config:
    defaults = dict(state_dir=tmp_path / "state", debug=False)
    defaults.update(overrides)
    return Config(**defaults)


def _file_handler():
    root = logging.getLogger("mcbridge")
    handlers = [
        h
        for h in root.handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(handlers) == 1
    return handlers[0]


def test_file_record_written_and_readable(tmp_path):
    cfg = _make_cfg(tmp_path)
    returned = setup_logging(cfg)

    expected = default_log_path(cfg)
    assert returned == expected
    assert expected.parent.is_dir()

    logging.getLogger("mcbridge.test").info("hello persistent world")

    content = expected.read_text(encoding="utf-8")
    assert "hello persistent world" in content
    # full-date format in the file
    assert re.search(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[INFO\] mcbridge\.test:",
        content,
    )


def test_console_and_file_both_receive_record(tmp_path):
    stream = StringIO()
    cfg = _make_cfg(tmp_path)
    setup_logging(cfg, stream=stream)

    logging.getLogger("mcbridge.test").info("mirrored everywhere")

    assert "mirrored everywhere" in stream.getvalue()
    file_handler = _file_handler()
    file_handler.flush()
    assert "mirrored everywhere" in default_log_path(cfg).read_text(
        encoding="utf-8"
    )


def test_console_format_uses_time_only(tmp_path):
    stream = StringIO()
    setup_logging(_make_cfg(tmp_path), stream=stream)

    logging.getLogger("mcbridge.test").info("tick")
    line = stream.getvalue()
    assert re.search(r"\d{2}:\d{2}:\d{2} \[INFO\] mcbridge\.test: tick", line)
    # no full date on the console (matches the pre-existing basicConfig look)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", line)


def test_rollover_gzips_rotated_files_and_respects_backup_count(tmp_path):
    # when="S" rotates on the first emit after each 1-second boundary —
    # the same lazy emit-time mechanism the production "midnight" handler
    # uses, just scaled down for tests.
    setup_logging(_make_cfg(tmp_path), rotate_when="S", backup_count=2)
    log_path = default_log_path(_make_cfg(tmp_path))
    _file_handler()

    log = logging.getLogger("mcbridge.test")
    for gen in range(1, 5):
        log.info("generation %d", gen)
        time.sleep(1.1)

    rotated = sorted(log_path.parent.glob("bridge.log.*"))
    assert len(rotated) == 2, "backupCount=2 must prune oldest rotated files"
    assert all(p.name.endswith(".gz") for p in rotated), (
        "rotated files must be gzipped"
    )
    assert log_path.exists()
    assert "generation 4" in log_path.read_text(encoding="utf-8")
    # the archives are real gzip and hold the rotated generations
    archived = "\n".join(
        gzip.open(p, "rt", encoding="utf-8").read() for p in rotated
    )
    assert "generation" in archived
    assert "generation 1" not in archived, (
        "oldest rotated file must have been pruned"
    )


def test_unwritable_dir_falls_back_to_console_only(tmp_path):
    stream = StringIO()
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the state dir should be")
    cfg = Config(state_dir=blocker, log_file=None, log_keep_days=14)

    returned = setup_logging(cfg, stream=stream)  # must not raise
    assert returned is None
    assert _file_handler_not_present()
    assert str(blocker) in stream.getvalue()
    assert "WARNING" in stream.getvalue()
    # console still works after the fallback
    logging.getLogger("mcbridge.test").warning("still talking")
    assert "still talking" in stream.getvalue()


def _file_handler_not_present() -> bool:
    root = logging.getLogger("mcbridge")
    return not any(
        isinstance(h, logging.handlers.TimedRotatingFileHandler)
        for h in root.handlers
    )


@pytest.mark.parametrize("bad", [0, -3])
def test_keep_days_clamped_to_one(tmp_path, bad):
    stream = StringIO()
    setup_logging(_make_cfg(tmp_path, log_keep_days=bad), stream=stream)
    assert _file_handler().backupCount == 1
    assert "clamping" in stream.getvalue()


def test_debug_sets_level_on_both_handlers(tmp_path):
    stream = StringIO()
    setup_logging(_make_cfg(tmp_path, debug=True), stream=stream)
    root = logging.getLogger("mcbridge")
    assert root.level == logging.DEBUG

    setup_logging(_make_cfg(tmp_path, debug=False), stream=stream)
    assert root.level == logging.INFO


def test_explicit_log_level_overrides_debug_flag(tmp_path):
    stream = StringIO()
    setup_logging(
        _make_cfg(tmp_path, debug=True, log_level=logging.WARNING),
        stream=stream,
    )
    assert logging.getLogger("mcbridge").level == logging.WARNING

    # and it actually filters: INFO records reach neither sink
    logging.getLogger("mcbridge.test").info("quiet please")
    assert "quiet please" not in stream.getvalue()
    assert "quiet please" not in default_log_path(
        _make_cfg(tmp_path)
    ).read_text(encoding="utf-8")


def test_log_file_override(tmp_path):
    custom = tmp_path / "custom" / "deep" / "bridge.log"
    cfg = _make_cfg(tmp_path, log_file=custom)
    returned = setup_logging(cfg)
    assert returned == custom
    assert custom.parent.is_dir()
    logging.getLogger("mcbridge.test").info("custom location")
    assert "custom location" in custom.read_text(encoding="utf-8")


# ------------------------------------------------------------------ config


def _write_toml(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_config_toml_log_options(tmp_path):
    toml = _write_toml(
        tmp_path,
        'log_file = "/tmp/override.log"\nlog_keep_days = 7\n',
    )
    cfg = load_config(["--config", toml])
    assert str(cfg.log_file) == "/tmp/override.log"
    assert cfg.log_keep_days == 7


def test_config_cli_overrides_toml(tmp_path):
    toml = _write_toml(
        tmp_path,
        'log_file = "/tmp/from-toml.log"\nlog_keep_days = 7\n',
    )
    cli_log = tmp_path / "from-cli.log"
    cfg = load_config(
        ["--config", toml, "--log-file", str(cli_log), "--log-keep-days", "3"]
    )
    assert cfg.log_file == cli_log
    assert cfg.log_keep_days == 3


def test_config_log_level_from_toml(tmp_path):
    toml = _write_toml(tmp_path, 'log_level = "warning"\n')
    cfg = load_config(["--config", toml])
    assert cfg.log_level == logging.WARNING
    assert cfg.debug is False


def test_config_cli_log_level_overrides_toml_and_is_case_insensitive(tmp_path):
    toml = _write_toml(tmp_path, 'log_level = "debug"\n')
    cfg = load_config(["--config", toml, "--log-level", "CRITICAL"])
    assert cfg.log_level == logging.CRITICAL


def test_config_invalid_toml_log_level_warns_and_ignores(tmp_path, caplog):
    toml = _write_toml(tmp_path, 'log_level = "loud"\n')
    with caplog.at_level(logging.WARNING, logger="mcbridge.config"):
        cfg = load_config(["--config", toml])
    assert cfg.log_level is None
    assert any("log_level" in r.message for r in caplog.records)


def test_config_rejects_unknown_cli_log_level(tmp_path):
    # argparse owns CLI validation: a typo should fail fast, not warn
    with pytest.raises(SystemExit):
        load_config(["--log-level", "loud"])


def test_config_invalid_toml_values_warn_and_ignore(tmp_path, caplog):
    toml = _write_toml(
        tmp_path,
        'log_file = 42\nlog_keep_days = "soon"\n',
    )
    with caplog.at_level(logging.WARNING, logger="mcbridge.config"):
        cfg = load_config(["--config", toml])
    assert cfg.log_file is None
    assert cfg.log_keep_days == 14
    assert any("log_file" in r.message for r in caplog.records)
    assert any("log_keep_days" in r.message for r in caplog.records)


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_config_clamps_nonsense_keep_days(tmp_path, caplog, bad):
    toml = _write_toml(tmp_path, f"log_keep_days = {bad}\n")
    with caplog.at_level(logging.WARNING, logger="mcbridge.config"):
        cfg = load_config(["--config", toml])
    assert cfg.log_keep_days == 1
    assert any("clamping" in r.message for r in caplog.records)
