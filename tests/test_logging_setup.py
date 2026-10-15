from __future__ import annotations

import io
import json
import logging
import sys
import warnings
from collections.abc import Iterator
from datetime import datetime

import pytest

from keyfinder.logging_setup import JsonFormatter, configure_logging

LOGGER_NAME = "keyfinder.tests.logging"


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """configure_logging mutates process-global state; put it back after every test."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_quiet = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(saved_level)
    for name, level in saved_quiet.items():
        logging.getLogger(name).setLevel(level)
    logging.captureWarnings(False)


def _ours() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if h.get_name() == "keyfinder"]


def _json_lines(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_json_lines_have_core_and_extra_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    logging.getLogger(LOGGER_NAME).info(
        "fetched %d terms", 3, extra={"host": "example.com", "attempt": 2, "ratio": 0.5}
    )

    (record,) = _json_lines(capsys.readouterr().err)
    assert record["level"] == "INFO"
    assert record["logger"] == LOGGER_NAME
    assert record["msg"] == "fetched 3 terms"
    assert record["host"] == "example.com"
    assert record["attempt"] == 2
    assert record["ratio"] == 0.5
    assert datetime.fromisoformat(str(record["ts"])).utcoffset() is not None
    # Standard LogRecord attributes are not leaked as fields.
    assert not {"args", "levelno", "pathname", "lineno", "msecs"} & record.keys()


def test_json_includes_exception_and_survives_unserialisable_extras(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG", "json")
    log = logging.getLogger(LOGGER_NAME)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("it failed", extra={"obj": object(), "weird": {(1, 2): "x"}, "nan": float("nan")})

    (record,) = _json_lines(capsys.readouterr().err)
    assert record["msg"] == "it failed"
    assert record["level"] == "ERROR"
    assert "RuntimeError: boom" in str(record["exc_info"])
    assert str(record["obj"]).startswith("<object object")
    assert isinstance(record["weird"], str)
    assert record["nan"] == "nan"


def test_extra_fields_cannot_clobber_core_keys() -> None:
    record = logging.LogRecord(LOGGER_NAME, logging.INFO, __file__, 1, "real message", None, None)
    record.ts = "forged"
    record.level = "forged"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "real message"
    assert payload["level"] == "INFO"
    assert payload["extra_level"] == "forged"
    assert payload["extra_ts"] == "forged"


def test_configuration_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    configure_logging("INFO", "json")
    configure_logging("INFO", "json")
    assert len(_ours()) == 1

    logging.getLogger(LOGGER_NAME).warning("once")
    assert len(_json_lines(capsys.readouterr().err)) == 1


def test_reconfiguring_switches_format_and_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", "json")
    configure_logging("warning", "text")
    assert len(_ours()) == 1
    assert logging.getLogger().level == logging.WARNING

    log = logging.getLogger(LOGGER_NAME)
    log.info("hidden")
    log.warning("shown", extra={"host": "example.com", "note": "two words"})

    err = capsys.readouterr().err
    assert "hidden" not in err
    (line,) = err.splitlines()
    assert "WARNING" in line
    assert f"{LOGGER_NAME}: shown" in line
    assert "host=example.com" in line
    assert 'note="two words"' in line
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_text_format_without_extras_has_no_trailing_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    logging.getLogger(LOGGER_NAME).info("plain")
    (line,) = capsys.readouterr().err.splitlines()
    assert line.endswith(f"{LOGGER_NAME}: plain")


def test_quiets_http_libraries() -> None:
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    configure_logging("DEBUG", "text")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_leaves_foreign_handlers_alone() -> None:
    foreign = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(foreign)
    try:
        configure_logging("INFO", "text")
        configure_logging("INFO", "json")
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)


def test_follows_sys_stderr_swapped_after_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_logging("INFO", "text")
    replacement = io.StringIO()
    monkeypatch.setattr(sys, "stderr", replacement)
    logging.getLogger(LOGGER_NAME).info("after swap")
    assert "after swap" in replacement.getvalue()


def test_routes_python_warnings_to_logging(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("deprecated thing", DeprecationWarning, stacklevel=1)
    records = _json_lines(capsys.readouterr().err)
    assert any(r["logger"] == "py.warnings" and "deprecated thing" in str(r["msg"]) for r in records)


@pytest.mark.parametrize("level", ["LOUD", "", "1"])
def test_rejects_unknown_level(level: str) -> None:
    with pytest.raises(ValueError, match="log level"):
        configure_logging(level)


def test_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="log format"):
        configure_logging("INFO", "xml")  # type: ignore[arg-type]
