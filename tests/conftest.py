from __future__ import annotations

from pathlib import Path

import pytest

from keyfinder.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings: temp data dir, no politeness delay, fast retries."""
    return Settings(
        data_dir=tmp_path / "data",
        request_interval=0,
        max_retries=1,
        request_timeout=5,
        log_level="WARNING",
    )
