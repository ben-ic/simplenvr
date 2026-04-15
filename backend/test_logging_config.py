"""Tests for the logging_config errors.log sink.

Covers:
  * Prod mode creates errors.log and sidecar.log in the same dir.
  * Only ERROR+ records land in errors.log; INFO/WARN are dropped.
  * sidecar.log still captures the full INFO+ stream.
  * Dev mode does NOT create errors.log (stdout-only by design).
  * Re-entering configure_logging() does not lower the errors handler
    off ERROR, even when other handlers get normalized to WARNING.
  * OSError on log-dir creation does not crash the sidecar — the
    handler is skipped and configure_logging() returns normally.

configure_logging() is global-state heavy (root logger handlers), so
every test uses a fixture that snapshots and restores root-logger state.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterator

import pytest

from backend import logging_config


@pytest.fixture
def fresh_configure(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], None]]:
    """Simulate a first-boot configure_logging() inside pytest.

    pytest's own logging plugin installs a LogCaptureHandler on the
    root logger *after* fixture setup but *before* the test body, so a
    fixture that just clears handlers and yields isn't enough — by the
    time the test calls configure_logging(), pytest's handler is back
    and the function hits its idempotent early-return path instead of
    the fresh-setup branch we want to exercise. Returning a callable
    means the test can clear handlers at the moment of configure(),
    after pytest has already done its thing.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    monkeypatch.delenv("SIMPLENVR_DEV", raising=False)

    def _call() -> None:
        for h in list(root.handlers):
            root.removeHandler(h)
        logging_config.configure_logging()

    try:
        yield _call
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def _point_log_dir_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect logging_config's log dir to a tmp path.

    platformdirs.user_log_dir is the only external path call in the
    module, so monkeypatching its name on the module's namespace is
    enough — we don't need a real platformdirs shim.
    """
    log_dir = tmp_path / "logs"

    class _FakePlatformdirs:
        @staticmethod
        def user_log_dir(appname: str, appauthor: str) -> str:  # noqa: ARG004
            return str(log_dir)

    monkeypatch.setattr(logging_config, "platformdirs", _FakePlatformdirs)
    return log_dir


def test_prod_creates_errors_log_alongside_sidecar_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_configure: Callable[[], None],
) -> None:
    log_dir = _point_log_dir_at(tmp_path, monkeypatch)

    fresh_configure()

    assert (log_dir / "sidecar.log").exists(), "sidecar.log should still be created in prod"
    assert (log_dir / "errors.log").exists(), "errors.log should be created in prod"


def test_errors_log_contains_only_error_level_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_configure: Callable[[], None],
) -> None:
    log_dir = _point_log_dir_at(tmp_path, monkeypatch)
    fresh_configure()

    log = logging.getLogger("test.errors_log")
    log.info("info-sentinel")
    log.warning("warning-sentinel")
    log.error("error-sentinel")
    # Flush every handler — RotatingFileHandler buffers aggressively
    # and without this the assertions race the write.
    for h in logging.getLogger().handlers:
        h.flush()

    errors_content = (log_dir / "errors.log").read_text()
    sidecar_content = (log_dir / "sidecar.log").read_text()

    assert "error-sentinel" in errors_content
    assert "info-sentinel" not in errors_content, (
        "errors.log leaked an INFO record — handler level not pinned to ERROR"
    )
    assert "warning-sentinel" not in errors_content, (
        "errors.log leaked a WARNING record — handler level not pinned to ERROR"
    )

    # sidecar.log keeps full forensic context — INFO and ERROR both land.
    assert "info-sentinel" in sidecar_content
    assert "error-sentinel" in sidecar_content


def test_dev_mode_does_not_create_errors_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_configure: Callable[[], None],
) -> None:
    log_dir = _point_log_dir_at(tmp_path, monkeypatch)
    monkeypatch.setenv("SIMPLENVR_DEV", "1")

    fresh_configure()

    # Dev mode is stdout-only — no on-disk logs at all, errors.log
    # included. The parent terminal already shows errors in the
    # stdout stream.
    assert not log_dir.exists() or not any(log_dir.iterdir()), (
        "dev mode should not create any log files on disk"
    )


def test_reentrant_call_does_not_lower_errors_handler_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_configure: Callable[[], None],
) -> None:
    _point_log_dir_at(tmp_path, monkeypatch)
    fresh_configure()

    root = logging.getLogger()
    err_handlers = [
        h for h in root.handlers if getattr(h, "_simplenvr_errors_only", False)
    ]
    assert len(err_handlers) == 1, "exactly one errors.log handler expected"
    err_handler = err_handlers[0]
    assert err_handler.level == logging.ERROR

    # Simulate a second configure_logging() call in prod — the early-
    # return normalizes levels to WARNING. The errors handler must
    # stay at ERROR, or a re-import at runtime would flood errors.log
    # with WARNING chatter.
    logging_config.configure_logging()

    assert err_handler.level == logging.ERROR, (
        "errors.log handler was lowered to WARNING by re-entrant configure_logging()"
    )


def test_errors_log_handler_missing_when_log_dir_unwritable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_configure: Callable[[], None],
) -> None:
    # Simulate disk-full / perms failure by pointing the log dir at a
    # path that cannot be created.
    unwritable = tmp_path / "not_a_dir"
    unwritable.write_text("i am a file, not a directory")

    class _FakePlatformdirs:
        @staticmethod
        def user_log_dir(appname: str, appauthor: str) -> str:  # noqa: ARG004
            # Requesting a subdir of a file triggers OSError on mkdir.
            return str(unwritable / "subdir")

    monkeypatch.setattr(logging_config, "platformdirs", _FakePlatformdirs)

    # Must not raise — the sidecar has to come up even without on-disk
    # logs. stderr-only fallback is the contract.
    fresh_configure()

    root = logging.getLogger()
    err_handlers = [
        h for h in root.handlers if getattr(h, "_simplenvr_errors_only", False)
    ]
    assert err_handlers == [], (
        "errors.log handler should not be registered when log dir is unwritable"
    )
