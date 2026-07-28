"""Daemon log date-rotation: midnight rollover into dated backups."""

from __future__ import annotations

import logging

import pytest

from packrat import paths
from packrat.daemon.__main__ import _setup_logging


@pytest.fixture()
def _clean_root_logging():
    """Snapshot + restore the root logger so tests don't leak handlers."""
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    for h in list(root.handlers):
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved:
        root.addHandler(h)
    root.setLevel(saved_level)


def _rotating_handler():
    from logging.handlers import TimedRotatingFileHandler

    for h in logging.getLogger().handlers:
        if isinstance(h, TimedRotatingFileHandler):
            return h
    raise AssertionError("no TimedRotatingFileHandler on root")


def test_setup_logging_writes_daemon_log(packrat_home, _clean_root_logging):
    _setup_logging()
    logging.getLogger("packrat.daemon").info("hello")
    _rotating_handler().flush()
    text = paths.daemon_log_path().read_text(encoding="utf-8")
    assert "hello" in text and "packrat.daemon" in text


def test_setup_logging_is_idempotent(packrat_home, _clean_root_logging):
    from logging.handlers import TimedRotatingFileHandler

    _setup_logging()
    _setup_logging()  # second call must not stack a second file handler
    n = sum(isinstance(h, TimedRotatingFileHandler) for h in logging.getLogger().handlers)
    assert n == 1


def test_rollover_produces_dated_backup(packrat_home, _clean_root_logging):
    import re

    _setup_logging()
    logging.getLogger("packrat.jobs").info("before midnight")
    handler = _rotating_handler()
    handler.doRollover()
    logging.getLogger("packrat.jobs").info("after midnight")
    handler.flush()

    backups = sorted(paths.logs_dir().glob("daemon.log.*"))
    assert len(backups) == 1
    assert re.search(r"daemon\.log\.\d{4}-\d{2}-\d{2}$", backups[0].name)
    # Old line in the dated backup, new line in the active file.
    assert "before midnight" in backups[0].read_text(encoding="utf-8")
    assert "after midnight" in paths.daemon_log_path().read_text(encoding="utf-8")


def test_bootstrap_log_path_is_distinct(packrat_home):
    assert paths.daemon_bootstrap_log_path() != paths.daemon_log_path()
    assert paths.daemon_bootstrap_log_path().name == "daemon-bootstrap.log"


def test_locked_rename_does_not_loop_forever(packrat_home, _clean_root_logging, monkeypatch):
    """A failed midnight rename must not strand rolloverAt (the 747 MB-flood bug).

    Simulate Windows WinError 32 by making the rename raise, then confirm the
    handler still advances past the rollover and keeps logging — instead of
    retrying the rollover (and dumping a traceback) on every subsequent record.
    """
    import os
    import time

    _setup_logging()
    handler = _rotating_handler()
    # Force shouldRollover() true, as it is at real midnight.
    handler.rolloverAt = int(time.time()) - 1

    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(
        OSError("[WinError 32] file in use")))
    handler.doRollover()  # must NOT raise despite the locked rename

    # rolloverAt now points to the FUTURE → shouldRollover won't re-fire on the next
    # record. This is the anti-loop invariant the stranded-rolloverAt bug violated.
    assert handler.rolloverAt > time.time()
    assert not list(paths.logs_dir().glob("daemon.log.*"))  # rename was skipped

    # Logging still works after the skipped rollover.
    logging.getLogger("packrat.jobs").info("after skipped rollover")
    handler.flush()
    assert "after skipped rollover" in paths.daemon_log_path().read_text(encoding="utf-8")


def test_setup_logging_quiets_uvicorn_access(packrat_home, _clean_root_logging):
    _setup_logging()
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
