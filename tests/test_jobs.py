from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from keyfinder import jobs as jobs_module
from keyfinder.cache import ResponseCache
from keyfinder.config import Settings
from keyfinder.http import HttpClient
from keyfinder.jobs import INTERNAL_ERROR, JobManager, JobManagerClosedError, QueueFullError
from keyfinder.models import AppSummary, ResearchParams, ResearchRun, RunStatus, Stage, Store
from keyfinder.storage import RunRepository
from keyfinder.stores import (
    StoreClient,
    StoreParseError,
    StoreRateLimitedError,
    StoreUnavailableError,
)


class FakeStore:
    """In-memory StoreClient: canned suggestions, optional gate and injected failure."""

    store = Store.APPSTORE
    supports_difficulty = True

    def __init__(self, *, gate: asyncio.Event | None = None, error: Exception | None = None) -> None:
        self.gate = gate
        self.error = error
        self.started = asyncio.Event()
        self.calls = 0

    async def suggest(self, term: str) -> list[str]:
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        if term == "photo":
            return ["photo", "photo editor", "photo collage"]
        return []

    async def top_apps(self, term: str, limit: int = 10) -> list[AppSummary] | None:
        return [AppSummary(app_id="1", title=f"{term} pro", ratings=1000)]


@dataclass
class Harness:
    manager: JobManager
    repo: RunRepository
    stores: list[FakeStore] = field(default_factory=list)


Factory = Callable[[ResearchParams, HttpClient, ResponseCache | None], StoreClient]


@pytest.fixture
async def make_manager(settings: Settings, tmp_path: Path) -> AsyncIterator[Callable[..., Harness]]:
    repo = RunRepository(tmp_path / "runs.db")
    await repo.init()
    http = HttpClient(settings)
    managers: list[JobManager] = []

    def build(store_maker: Callable[[], FakeStore] = FakeStore, max_concurrent_runs: int = 2) -> Harness:
        harness_stores: list[FakeStore] = []

        def factory(params: ResearchParams, http: HttpClient, cache: ResponseCache | None) -> StoreClient:
            store = store_maker()
            harness_stores.append(store)
            return store

        cfg = settings.model_copy(update={"max_concurrent_runs": max_concurrent_runs})
        manager = JobManager(cfg, repo, http, None, client_factory=factory)
        managers.append(manager)
        return Harness(manager, repo, harness_stores)

    yield build
    for manager in managers:
        await manager.shutdown()
    await http.aclose()
    repo.close()


def params(seed: str = "photo") -> ResearchParams:
    return ResearchParams(seed=seed, limit=10, probe=True, difficulty=True)


async def wait_finished(manager: JobManager, run_id: str, timeout: float = 5.0) -> ResearchRun:
    async def poll() -> ResearchRun:
        while True:
            run = await manager.get(run_id)
            assert run is not None
            if run.status.finished and not manager.is_active(run_id):
                return run
            await asyncio.sleep(0.005)

    return await asyncio.wait_for(poll(), timeout)


async def test_submit_runs_to_success(make_manager: Callable[..., Harness]) -> None:
    h = make_manager()
    run = await h.manager.submit(params())
    assert run.status is RunStatus.PENDING
    assert len(run.id) == 32
    stored = await h.repo.get(run.id)
    assert stored is not None and stored.status in (RunStatus.PENDING, RunStatus.RUNNING)

    final = await wait_finished(h.manager, run.id)
    assert final.status is RunStatus.SUCCEEDED
    assert final.error is None
    assert final.started_at is not None and final.finished_at is not None
    assert final.progress.stage is Stage.DONE
    assert {r.keyword for r in final.results} == {"photo", "photo editor", "photo collage"}
    assert all(r.difficulty is not None for r in final.results)
    assert await h.repo.get(run.id) == final
    assert h.manager.active_count == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (StoreUnavailableError("search.itunes.apple.com is unavailable (HTTP 503; 2 attempts)"), "reached"),
        (StoreRateLimitedError("itunes.apple.com kept rate limiting (HTTP 429)"), "rate-limiting"),
        (StoreParseError("unrecognised payload"), "unexpected format"),
    ],
)
async def test_store_error_fails_with_friendly_message(
    make_manager: Callable[..., Harness], error: Exception, expected: str
) -> None:
    h = make_manager(lambda: FakeStore(error=error))
    run = await h.manager.submit(params())
    final = await wait_finished(h.manager, run.id)
    assert final.status is RunStatus.FAILED
    assert final.error is not None
    assert expected in final.error
    assert str(error) in final.error
    assert final.results == []


async def test_unexpected_error_is_internal_error(
    make_manager: Callable[..., Harness], caplog: pytest.LogCaptureFixture
) -> None:
    h = make_manager(lambda: FakeStore(error=KeyError("secret detail")))
    run = await h.manager.submit(params())
    final = await wait_finished(h.manager, run.id)
    assert final.status is RunStatus.FAILED
    assert final.error == INTERNAL_ERROR
    assert any(record.exc_info for record in caplog.records if "unexpected" in record.getMessage())


async def test_factory_value_error_propagates(make_manager: Callable[..., Harness]) -> None:
    def bad_store() -> FakeStore:
        raise ValueError("unknown App Store country 'zz'")

    h = make_manager(bad_store)
    with pytest.raises(ValueError, match="zz"):
        await h.manager.submit(params())
    assert await h.repo.list() == []
    assert h.manager.active_count == 0


async def test_cancel_running_run(make_manager: Callable[..., Harness]) -> None:
    gate = asyncio.Event()
    h = make_manager(lambda: FakeStore(gate=gate))
    run = await h.manager.submit(params())
    await asyncio.wait_for(_started(h), 5)
    live = await h.manager.get(run.id)
    assert live is not None and live.status is RunStatus.RUNNING

    assert await h.manager.cancel(run.id) is True
    assert not h.manager.is_active(run.id)
    stored = await h.repo.get(run.id)
    assert stored is not None
    assert stored.status is RunStatus.CANCELLED
    assert stored.finished_at is not None
    assert await h.manager.cancel(run.id) is False
    assert await h.manager.cancel("unknown") is False


async def test_concurrency_limit_queues_runs(make_manager: Callable[..., Harness]) -> None:
    gate = asyncio.Event()
    h = make_manager(lambda: FakeStore(gate=gate), max_concurrent_runs=1)
    first = await h.manager.submit(params())
    second = await h.manager.submit(params("video"))
    await asyncio.wait_for(h.stores[0].started.wait(), 5)
    await asyncio.sleep(0.05)

    first_live = await h.manager.get(first.id)
    second_live = await h.manager.get(second.id)
    assert first_live is not None and first_live.status is RunStatus.RUNNING
    assert second_live is not None and second_live.status is RunStatus.PENDING
    assert second_live.started_at is None
    assert h.stores[1].calls == 0
    assert h.manager.active_count == 2

    gate.set()
    assert (await wait_finished(h.manager, first.id)).status is RunStatus.SUCCEEDED
    assert (await wait_finished(h.manager, second.id)).status is RunStatus.SUCCEEDED


async def test_cancel_pending_run(make_manager: Callable[..., Harness]) -> None:
    gate = asyncio.Event()
    h = make_manager(lambda: FakeStore(gate=gate), max_concurrent_runs=1)
    first = await h.manager.submit(params())
    queued = await h.manager.submit(params("video"))
    await asyncio.wait_for(h.stores[0].started.wait(), 5)

    assert await h.manager.cancel(queued.id) is True
    stored = await h.repo.get(queued.id)
    assert stored is not None
    assert stored.status is RunStatus.CANCELLED
    assert stored.started_at is None
    assert h.stores[1].calls == 0

    gate.set()
    assert (await wait_finished(h.manager, first.id)).status is RunStatus.SUCCEEDED


async def test_get_returns_snapshot_copy(make_manager: Callable[..., Harness]) -> None:
    gate = asyncio.Event()
    h = make_manager(lambda: FakeStore(gate=gate))
    run = await h.manager.submit(params())
    snapshot = await h.manager.get(run.id)
    assert snapshot is not None
    snapshot.status = RunStatus.FAILED
    again = await h.manager.get(run.id)
    assert again is not None and again.status is not RunStatus.FAILED
    assert await h.manager.get("unknown") is None
    gate.set()
    await wait_finished(h.manager, run.id)


async def test_progress_is_persisted_while_running(
    make_manager: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs_module, "PROGRESS_PERSIST_INTERVAL", 0.0)
    gate = asyncio.Event()
    h = make_manager(lambda: FakeStore(gate=gate))
    run = await h.manager.submit(params())
    await asyncio.wait_for(_started(h), 5)
    await asyncio.sleep(0.05)
    stored = await h.repo.get(run.id)
    assert stored is not None
    assert stored.status is RunStatus.RUNNING
    assert stored.started_at is not None
    assert stored.progress.stage is Stage.EXPANDING
    assert stored.progress.total == 27
    gate.set()
    await wait_finished(h.manager, run.id)


async def test_shutdown_cancels_active_runs(make_manager: Callable[..., Harness]) -> None:
    gate = asyncio.Event()
    h = make_manager(lambda: FakeStore(gate=gate), max_concurrent_runs=1)
    running = await h.manager.submit(params())
    pending = await h.manager.submit(params("video"))
    await asyncio.wait_for(h.stores[0].started.wait(), 5)

    await h.manager.shutdown()
    assert h.manager.active_count == 0
    for run_id in (running.id, pending.id):
        stored = await h.repo.get(run_id)
        assert stored is not None
        assert stored.status is RunStatus.CANCELLED
    with pytest.raises(JobManagerClosedError):
        await h.manager.submit(params())


async def test_queue_limit(make_manager: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs_module, "MAX_QUEUED_RUNS", 1)
    gate = asyncio.Event()
    h = make_manager(lambda: FakeStore(gate=gate))
    run = await h.manager.submit(params())
    with pytest.raises(QueueFullError):
        await h.manager.submit(params("video"))
    gate.set()
    await wait_finished(h.manager, run.id)
    await h.manager.submit(params("video"))


async def _started(h: Harness) -> None:
    while not h.stores:
        await asyncio.sleep(0)
    await h.stores[0].started.wait()
