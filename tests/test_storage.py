from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from keyfinder.models import (
    AppSummary,
    KeywordResult,
    Progress,
    ResearchParams,
    ResearchRun,
    RunStatus,
    Stage,
    Store,
)
from keyfinder.storage import INTERRUPTED_ERROR, RunRepository

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def make_run(run_id: str, *, minutes: int = 0, status: RunStatus = RunStatus.SUCCEEDED) -> ResearchRun:
    results = [
        KeywordResult(
            keyword="photo editor",
            popularity=80,
            difficulty=40,
            opportunity=48,
            chars_typed=3,
            position=1,
            top_apps=[AppSummary(app_id="1", title="Photo Pro", developer="Ex", ratings=10, rating=4.5)],
        ),
        KeywordResult(keyword="photo editor free", popularity=60),
    ]
    finished = status.finished
    return ResearchRun(
        id=run_id,
        params=ResearchParams(seed="Photo  Editor", store=Store.APPSTORE, country="gb", limit=20),
        status=status,
        created_at=BASE + timedelta(minutes=minutes),
        started_at=BASE + timedelta(minutes=minutes) if status is not RunStatus.PENDING else None,
        finished_at=BASE + timedelta(minutes=minutes, seconds=30) if finished else None,
        progress=Progress(stage=Stage.DONE if finished else Stage.PROBING, keywords=7, requests=12),
        results=results if status is RunStatus.SUCCEEDED else [],
        error="boom" if status is RunStatus.FAILED else None,
    )


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """A direct connection that commits on success and is always closed (sqlite3's own
    context manager only commits, leaving the connection open)."""
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RunRepository]:
    repository = RunRepository(tmp_path / "nested" / "runs.db")
    await repository.init()
    yield repository
    repository.close()


async def test_init_creates_parent_dir_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "runs.db"
    repository = RunRepository(path)
    await repository.init()
    await repository.init()
    assert path.exists()
    with connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(runs)")}
    assert {
        "id",
        "created_at",
        "status",
        "seed",
        "store",
        "country",
        "keyword_count",
        "error",
        "data",
    } <= columns
    assert "runs_created_at" in indexes
    repository.close()


async def test_save_get_roundtrip(repo: RunRepository) -> None:
    run = make_run("r1")
    await repo.save(run)
    loaded = await repo.get("r1")
    assert loaded == run
    assert await repo.get("missing") is None


async def test_save_upserts(repo: RunRepository) -> None:
    run = make_run("r1", status=RunStatus.RUNNING)
    await repo.save(run)
    run.status = RunStatus.SUCCEEDED
    run.finished_at = BASE + timedelta(hours=1)
    run.results = [KeywordResult(keyword="x", popularity=10)]
    await repo.save(run)
    loaded = await repo.get("r1")
    assert loaded is not None
    assert loaded.status is RunStatus.SUCCEEDED
    assert [r.keyword for r in loaded.results] == ["x"]
    summaries = await repo.list()
    assert len(summaries) == 1
    assert summaries[0].keyword_count == 1
    assert summaries[0].finished_at == run.finished_at


async def test_list_newest_first_with_pagination(repo: RunRepository) -> None:
    for i, minutes in enumerate([5, 1, 9, 3]):
        await repo.save(make_run(f"r{i}", minutes=minutes))
    summaries = await repo.list()
    assert [s.id for s in summaries] == ["r2", "r0", "r3", "r1"]
    assert [s.id for s in await repo.list(limit=2)] == ["r2", "r0"]
    assert [s.id for s in await repo.list(limit=2, offset=2)] == ["r3", "r1"]
    assert await repo.list(offset=10) == []
    first = summaries[0]
    assert first.params.seed == "photo editor"
    assert first.params.country == "gb"
    assert first.status is RunStatus.SUCCEEDED
    assert first.created_at == BASE + timedelta(minutes=9)
    assert first.keyword_count == 2


async def test_list_keyword_count_uses_progress_while_active(repo: RunRepository) -> None:
    await repo.save(make_run("active", status=RunStatus.RUNNING))
    (summary,) = await repo.list()
    assert summary.keyword_count == 7
    assert summary.finished_at is None


async def test_list_rejects_bad_paging(repo: RunRepository) -> None:
    with pytest.raises(ValueError):
        await repo.list(limit=0)
    with pytest.raises(ValueError):
        await repo.list(offset=-1)


async def test_delete(repo: RunRepository) -> None:
    await repo.save(make_run("r1"))
    assert await repo.delete("r1") is True
    assert await repo.delete("r1") is False
    assert await repo.get("r1") is None
    assert await repo.list() == []


async def test_mark_interrupted(repo: RunRepository) -> None:
    await repo.save(make_run("pending", status=RunStatus.PENDING, minutes=1))
    await repo.save(make_run("running", status=RunStatus.RUNNING, minutes=2))
    await repo.save(make_run("done", minutes=3))
    await repo.save(make_run("failed", status=RunStatus.FAILED, minutes=4))

    assert await repo.mark_interrupted() == 2
    assert await repo.mark_interrupted() == 0

    for run_id in ("pending", "running"):
        run = await repo.get(run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert run.error == INTERRUPTED_ERROR
        assert run.finished_at is not None
    done = await repo.get("done")
    failed = await repo.get("failed")
    assert done is not None and done.status is RunStatus.SUCCEEDED
    assert failed is not None and failed.error == "boom"
    statuses = {s.id: (s.status, s.error) for s in await repo.list()}
    assert statuses["running"] == (RunStatus.FAILED, INTERRUPTED_ERROR)


async def test_reopen_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    first = RunRepository(path)
    await first.init()
    await first.save(make_run("r1"))
    first.close()

    second = RunRepository(path)
    await second.init()
    try:
        assert await second.get("r1") == make_run("r1")
        assert [s.id for s in await second.list()] == ["r1"]
    finally:
        second.close()


async def test_unreadable_row_is_skipped(repo: RunRepository) -> None:
    await repo.save(make_run("good", minutes=1))
    await repo.save(make_run("bad", minutes=2))
    with connect(repo.path) as conn:
        conn.execute("UPDATE runs SET data = '{not json', params = 'nope' WHERE id = 'bad'")
    assert await repo.get("bad") is None
    assert [s.id for s in await repo.list()] == ["good"]


async def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    with connect(path) as conn:
        conn.execute("PRAGMA user_version = 99")
    repository = RunRepository(path)
    with pytest.raises(RuntimeError, match="newer"):
        await repository.init()


async def test_use_before_init_and_after_close(tmp_path: Path) -> None:
    repository = RunRepository(tmp_path / "runs.db")
    with pytest.raises(RuntimeError, match="init"):
        await repository.get("x")
    await repository.init()
    repository.close()
    repository.close()
    with pytest.raises(RuntimeError, match="closed"):
        await repository.list()
    with pytest.raises(RuntimeError, match="closed"):
        await repository.init()
