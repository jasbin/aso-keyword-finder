from __future__ import annotations

from pathlib import Path

import pytest

from keyfinder.config import Settings


def test_empty_env_values_mean_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("KEYFINDER_API_KEY", "")
    monkeypatch.setenv("KEYFINDER_PORT", "")
    settings = Settings()
    assert settings.api_key is None  # not SecretStr(""): auth stays off instead of half-on
    assert settings.port == 8000


def test_env_values_are_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEYFINDER_API_KEY", "s3cret")
    monkeypatch.setenv("KEYFINDER_DATA_DIR", str(tmp_path / "d"))
    settings = Settings()
    assert settings.api_key is not None and settings.api_key.get_secret_value() == "s3cret"
    assert settings.db_path == tmp_path / "d" / "keyfinder.db"
    assert settings.cache_path == tmp_path / "d" / "cache.db"
