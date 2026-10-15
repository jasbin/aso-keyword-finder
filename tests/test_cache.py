from __future__ import annotations

import asyncio
import sqlite3
import time as real_time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

import keyfinder.cache as cache_module
from keyfinder.cache import ResponseCache


class FakeWallClock:
    """Stands in for the ``time`` module inside keyfinder.cache."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeWallClock:
    fake = FakeWallClock()
    monkeypatch.setattr(cache_module, "time", fake)
    return fake


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "nested" / "dir" / "cache.db"


@pytest.fixture
async def cache(cache_path: Path) -> AsyncIterator[ResponseCache]:
    instance = ResponseCache(cache_path, ttl_seconds=60)
    yield instance
    instance.close()


@pytest.fixture
def raw_db(cache_path: Path, cache: ResponseCache) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(cache_path)
    yield conn
    conn.close()


async def test_round_trips_json_values(cache: ResponseCache) -> None:
    values = {
        "list": ["meditation", "meditation app"],
        "dict": {"app_id": "1", "title": "Calm", "rating": 4.8, "url": None},
        "records": [{"ratings": 10}, {"ratings": 20}],
        "text": "ünïcode ✓",
        "number": 42,
        "empty": [],
    }
    for key, value in values.items():
        await cache.set("ns", key, value)
    for key, value in values.items():
        assert await cache.get("ns", key) == value


async def test_missing_key_and_namespace_isolation(cache: ResponseCache) -> None:
    assert await cache.get("ns", "nope") is None
    await cache.set("appstore:us:en:suggest", "yoga", ["yoga"])
    assert await cache.get("play:us:en:suggest", "yoga") is None
    assert await cache.get("appstore:us:en:suggest", "yoga") == ["yoga"]


async def test_overwrite_replaces_value_and_refreshes_expiry(
    cache: ResponseCache, clock: FakeWallClock
) -> None:
    await cache.set("ns", "k", "old")
    clock.now += 50
    await cache.set("ns", "k", "new")
    clock.now += 50  # 100s after the first write, 50s after the second
    assert await cache.get("ns", "k") == "new"
    assert await cache.stats() == {"entries": 1, "expired": 0}


async def test_entries_expire_after_ttl(cache: ResponseCache, clock: FakeWallClock) -> None:
    await cache.set("ns", "k", [1, 2, 3])
    clock.now += 59
    assert await cache.get("ns", "k") == [1, 2, 3]
    clock.now += 1  # exactly at expires_at counts as expired
    assert await cache.get("ns", "k") is None
    assert await cache.stats() == {"entries": 1, "expired": 1}


async def test_ttl_zero_disables_reads_and_writes(cache_path: Path) -> None:
    disabled = ResponseCache(cache_path, ttl_seconds=0)
    try:
        assert not disabled.enabled
        await disabled.set("ns", "k", "v")
        assert await disabled.get("ns", "k") is None
        assert await disabled.stats() == {"entries": 0, "expired": 0}
    finally:
        disabled.close()


async def test_disabled_cache_can_still_clear_existing_file(cache_path: Path) -> None:
    enabled = ResponseCache(cache_path, ttl_seconds=60)
    await enabled.set("ns", "k", "v")
    enabled.close()

    disabled = ResponseCache(cache_path, ttl_seconds=0)
    try:
        assert await disabled.get("ns", "k") is None
        assert await disabled.stats() == {"entries": 1, "expired": 0}
        assert await disabled.clear() == 1
    finally:
        disabled.close()


def test_negative_ttl_is_rejected(cache_path: Path) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        ResponseCache(cache_path, ttl_seconds=-1)


async def test_purge_expired_removes_only_expired(cache: ResponseCache, clock: FakeWallClock) -> None:
    await cache.set("ns", "old-1", 1)
    await cache.set("ns", "old-2", 2)
    clock.now += 30
    await cache.set("ns", "fresh", 3)
    clock.now += 31  # old-* expired, fresh has 29s left

    assert await cache.stats() == {"entries": 3, "expired": 2}
    assert await cache.purge_expired() == 2
    assert await cache.purge_expired() == 0
    assert await cache.stats() == {"entries": 1, "expired": 0}
    assert await cache.get("ns", "fresh") == 3


async def test_clear_removes_everything(cache: ResponseCache) -> None:
    for i in range(5):
        await cache.set(f"ns{i % 2}", str(i), i)
    assert await cache.clear() == 5
    assert await cache.clear() == 0
    assert await cache.stats() == {"entries": 0, "expired": 0}
    assert await cache.get("ns0", "0") is None


async def test_concurrent_get_and_set_from_many_tasks(cache: ResponseCache) -> None:
    async def worker(i: int) -> tuple[int, object]:
        await cache.set("ns", f"key-{i}", {"i": i, "terms": [f"t{i}"] * 3})
        await cache.set("shared", "hot", i)
        return i, await cache.get("ns", f"key-{i}")

    results = await asyncio.gather(*(worker(i) for i in range(200)))

    assert all(value == {"i": i, "terms": [f"t{i}"] * 3} for i, value in results)
    assert await cache.get("shared", "hot") in range(200)
    assert await cache.stats() == {"entries": 201, "expired": 0}


async def test_survives_reopen(cache_path: Path) -> None:
    first = ResponseCache(cache_path, ttl_seconds=3600)
    await first.set("ns", "k", {"terms": ["a", "b"]})
    first.close()

    second = ResponseCache(cache_path, ttl_seconds=3600)
    try:
        assert await second.get("ns", "k") == {"terms": ["a", "b"]}
    finally:
        second.close()


async def test_expiry_survives_reopen(cache_path: Path, clock: FakeWallClock) -> None:
    first = ResponseCache(cache_path, ttl_seconds=10)
    await first.set("ns", "k", "v")
    first.close()

    clock.now += 11
    second = ResponseCache(cache_path, ttl_seconds=3600)  # a longer TTL does not revive old entries
    try:
        assert await second.get("ns", "k") is None
    finally:
        second.close()


async def test_creates_parent_dirs_and_uses_wal(cache_path: Path, raw_db: sqlite3.Connection) -> None:
    assert cache_path.parent.is_dir()
    (mode,) = raw_db.execute("PRAGMA journal_mode").fetchone()
    assert mode == "wal"
    columns = [row[1] for row in raw_db.execute("PRAGMA table_info(cache)")]
    assert columns == ["namespace", "key", "value", "expires_at"]


async def test_stores_absolute_wall_clock_expiry(cache: ResponseCache, raw_db: sqlite3.Connection) -> None:
    before = real_time.time()
    await cache.set("ns", "k", "v")
    (expires_at,) = raw_db.execute("SELECT expires_at FROM cache").fetchone()
    assert before + 60 <= expires_at <= real_time.time() + 60


async def test_undecodable_entry_is_a_miss(cache: ResponseCache, raw_db: sqlite3.Connection) -> None:
    raw_db.execute(
        "INSERT INTO cache VALUES (?, ?, ?, ?)", ("ns", "bad", "{not json", real_time.time() + 600)
    )
    raw_db.commit()
    assert await cache.get("ns", "bad") is None


async def test_rejects_non_json_values(cache: ResponseCache) -> None:
    with pytest.raises(TypeError):
        await cache.set("ns", "k", object())
    with pytest.raises(ValueError):
        await cache.set("ns", "k", float("nan"))
    assert await cache.stats() == {"entries": 0, "expired": 0}


async def test_close_is_idempotent_and_blocks_further_use(cache_path: Path) -> None:
    instance = ResponseCache(cache_path, ttl_seconds=60)
    instance.close()
    instance.close()
    with pytest.raises(RuntimeError, match="closed"):
        await instance.get("ns", "k")
    with pytest.raises(RuntimeError, match="closed"):
        await instance.stats()
