"""Background execution of research runs for the API server.

Each submitted run gets its own asyncio task. A semaphore of ``settings.max_concurrent_runs``
limits how many execute at once; the rest wait with status PENDING. The live run object is
kept in memory while active (progress is persisted at most once per second) and the final
state is always written to the :class:`~keyfinder.storage.RunRepository`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

from keyfinder.cache import ResponseCache
from keyfinder.config import Settings
from keyfinder.http import HttpClient
from keyfinder.models import Progress, ResearchParams, ResearchRun, RunStatus, Stage, utcnow
from keyfinder.research import ResearchEngine
from keyfinder.storage import RunRepository
from keyfinder.stores import (
    CachedStoreClient,
    StoreClient,
    StoreClientFactory,
    StoreError,
    StoreParseError,
    StoreRateLimitedError,
    create_store_client,
)

__all__ = [
    "INTERNAL_ERROR",
    "MAX_QUEUED_RUNS",
    "PROGRESS_PERSIST_INTERVAL",
    "JobManager",
    "JobManagerClosedError",
    "QueueFullError",
]

logger = logging.getLogger(__name__)

PROGRESS_PERSIST_INTERVAL = 1.0
"""Minimum seconds between two progress writes of one run to the repository."""

MAX_QUEUED_RUNS = 100
"""Upper bound on active (pending + running) runs; further submissions are refused."""

INTERNAL_ERROR = "internal error"
"""Error shown to users when a run fails for a reason other than a store error."""


class JobManagerClosedError(RuntimeError):
    """The manager is shutting down and accepts no new runs."""


class QueueFullError(RuntimeError):
    """Too many runs are already pending or running."""


@dataclass
class _Job:
    run: ResearchRun
    client: StoreClient
    task: asyncio.Task[None] | None = None
    last_persist: float = float("-inf")
    request_baseline: int | None = None  # http.stats.requests when the run started


def _friendly_error(exc: StoreError) -> str:
    """A message suitable for end users; store error messages never contain query strings."""
    detail = str(exc) or type(exc).__name__
    if isinstance(exc, StoreRateLimitedError):
        return f"The store is rate-limiting requests; try again later. ({detail})"
    if isinstance(exc, StoreParseError):
        return f"The store returned a response in an unexpected format; its API may have changed. ({detail})"
    return f"The store could not be reached. ({detail})"


class JobManager:
    """Runs research jobs in the background and tracks them until they finish."""

    def __init__(
        self,
        settings: Settings,
        repo: RunRepository,
        http: HttpClient,
        cache: ResponseCache | None,
        *,
        client_factory: StoreClientFactory = create_store_client,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._http = http
        self._cache = cache
        self._client_factory = client_factory
        self._slots = asyncio.Semaphore(settings.max_concurrent_runs)
        # Strong references to every active job (and its task) until it finishes.
        self._jobs: dict[str, _Job] = {}
        self._closing = False

    @property
    def active_count(self) -> int:
        """Number of runs that are pending or running."""
        return len(self._jobs)

    def is_active(self, run_id: str) -> bool:
        """True while ``run_id`` is pending or running in this manager."""
        return run_id in self._jobs

    async def submit(self, params: ResearchParams) -> ResearchRun:
        """Persist a PENDING run, start it in the background and return a snapshot of it.

        Raises ``ValueError`` when no store client can be built for ``params`` (e.g. an
        unknown App Store country), :class:`QueueFullError` when too many runs are active
        and :class:`JobManagerClosedError` during shutdown.
        """
        if self._closing:
            raise JobManagerClosedError("the server is shutting down")
        if len(self._jobs) >= MAX_QUEUED_RUNS:
            raise QueueFullError(f"too many active runs (limit {MAX_QUEUED_RUNS}); try again later")
        client = self._client_factory(params, self._http, self._cache)
        run = ResearchRun(id=uuid.uuid4().hex, params=params)
        await self._repo.save(run)
        if self._closing:  # shutdown started while saving
            await self._finish_cancelled(_Job(run=run, client=client))
            raise JobManagerClosedError("the server is shutting down")
        job = _Job(run=run, client=client)
        self._jobs[run.id] = job
        job.task = asyncio.create_task(self._execute(job), name=f"research-run-{run.id[:8]}")
        logger.info(
            "run %s submitted: seed=%r store=%s country=%s",
            run.id,
            params.seed,
            params.store.value,
            params.country,
            extra={"run_id": run.id},
        )
        return run.model_copy(deep=True)

    async def get(self, run_id: str) -> ResearchRun | None:
        """The live state of an active run, otherwise the stored run (None if unknown)."""
        job = self._jobs.get(run_id)
        if job is not None:
            return job.run.model_copy(deep=True)
        return await self._repo.get(run_id)

    async def cancel(self, run_id: str) -> bool:
        """Cancel an active run and wait until it is stored as CANCELLED.

        Returns False when the run is not active (unknown or already finished).
        """
        job = self._jobs.get(run_id)
        if job is None or job.task is None or job.task.done():
            return False
        if not job.task.cancel():
            return False
        logger.info("run %s cancellation requested", run_id, extra={"run_id": run_id})
        await asyncio.wait({job.task})
        await self._reap(job)
        return job.run.status is RunStatus.CANCELLED

    async def shutdown(self) -> None:
        """Refuse new runs, cancel the active ones and wait until they are stored as CANCELLED."""
        self._closing = True
        jobs = list(self._jobs.values())
        if not jobs:
            return
        logger.info("cancelling %d active run(s) for shutdown", len(jobs))
        tasks = [job.task for job in jobs if job.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in jobs:
            await self._reap(job)

    async def _reap(self, job: _Job) -> None:
        """Finalize a job whose task was cancelled before its first step (its handlers never ran)."""
        if self._jobs.get(job.run.id) is job:
            await self._finish_cancelled(job)

    # ------------------------------------------------------------------ execution

    async def _execute(self, job: _Job) -> None:
        run = job.run
        try:
            async with self._slots:
                await self._start(job)
                results = await self._engine(job).run()
            run.results = results
            run.status = RunStatus.SUCCEEDED
            run.finished_at = utcnow()
            run.progress.stage = Stage.DONE
            logger.info("run %s succeeded with %d keywords", run.id, len(results), extra={"run_id": run.id})
        except asyncio.CancelledError:
            await self._finish_cancelled(job)
            if self._closing:
                raise
            return
        except StoreError as exc:
            run.status = RunStatus.FAILED
            run.error = _friendly_error(exc)
            run.finished_at = utcnow()
            logger.warning("run %s failed: %s", run.id, exc, extra={"run_id": run.id})
        except Exception:
            run.status = RunStatus.FAILED
            run.error = INTERNAL_ERROR
            run.finished_at = utcnow()
            logger.exception("run %s failed with an unexpected error", run.id, extra={"run_id": run.id})
        self._final_counters(job)
        await self._persist_final(run)

    async def _start(self, job: _Job) -> None:
        run = job.run
        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        await self._repo.save(run)
        job.last_persist = time.monotonic()
        logger.info("run %s started", run.id, extra={"run_id": run.id})

    def _engine(self, job: _Job) -> ResearchEngine:
        # http.stats is shared by every run, so when runs overlap this per-run delta also counts
        # requests made by the other runs in the meantime: it is approximate by design.
        baseline = self._http.stats.requests
        job.request_baseline = baseline
        client = job.client

        async def on_progress(progress: Progress) -> None:
            job.run.progress = progress
            now = time.monotonic()
            if now - job.last_persist >= PROGRESS_PERSIST_INTERVAL:
                job.last_persist = now
                await self._repo.save(job.run)

        return ResearchEngine(
            client,
            job.run.params,
            on_progress=on_progress,
            concurrency=self._settings.max_concurrency,
            request_counter=lambda: self._http.stats.requests - baseline,
            cache_hits=(lambda: client.hits) if isinstance(client, CachedStoreClient) else None,
        )

    def _final_counters(self, job: _Job) -> None:
        """Bring progress counters up to date: the last progress emission can predate the
        final requests (e.g. the retries of the request that made the run fail)."""
        if job.request_baseline is not None:
            requests = self._http.stats.requests - job.request_baseline
            job.run.progress.requests = max(job.run.progress.requests, requests)
        if isinstance(job.client, CachedStoreClient):
            job.run.progress.cache_hits = max(job.run.progress.cache_hits, job.client.hits)

    async def _finish_cancelled(self, job: _Job) -> None:
        self._final_counters(job)
        run = job.run
        run.status = RunStatus.CANCELLED
        run.finished_at = utcnow()
        logger.info("run %s cancelled", run.id, extra={"run_id": run.id})
        await self._persist_final(run)

    async def _persist_final(self, run: ResearchRun) -> None:
        """Store the final state, then forget the job (readers switch to the repository)."""
        try:
            # Shielded: a further cancellation must not lose the final state.
            await asyncio.shield(self._repo.save(run))
        except Exception:
            logger.exception("could not store the final state of run %s", run.id, extra={"run_id": run.id})
        finally:
            self._jobs.pop(run.id, None)
