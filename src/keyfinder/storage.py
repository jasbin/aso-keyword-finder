"""SQLite-backed history of research runs, shared by the CLI and the API server."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from keyfinder.models import ResearchParams, ResearchRun, RunStatus, RunSummary, utcnow

__all__ = ["INTERRUPTED_ERROR", "MAX_LIST_LIMIT", "RunRepository"]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

INTERRUPTED_ERROR = "interrupted by restart"
"""Error recorded on runs that were still pending/running when the process stopped."""

MAX_LIST_LIMIT = 1000
"""Largest page size :meth:`RunRepository.list` accepts."""

# Bump together with a migration step in ``_migrate`` when the schema changes.
_SCHEMA_VERSION = 1

_SCHEMA_V1 = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        id            TEXT PRIMARY KEY,
        created_at    REAL NOT NULL,
        finished_at   REAL,
        status        TEXT NOT NULL,
        seed          TEXT NOT NULL,
        store         TEXT NOT NULL,
        country       TEXT NOT NULL,
        keyword_count INTEGER NOT NULL DEFAULT 0,
        error         TEXT,
        params        TEXT NOT NULL,
        data          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS runs_created_at ON runs (created_at)",
    "CREATE INDEX IF NOT EXISTS runs_status ON runs (status)",
)

_UPSERT = """
    INSERT INTO runs (id, created_at, finished_at, status, seed, store, country,
                      keyword_count, error, params, data)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (id) DO UPDATE SET
        created_at = excluded.created_at,
        finished_at = excluded.finished_at,
        status = excluded.status,
        seed = excluded.seed,
        store = excluded.store,
        country = excluded.country,
        keyword_count = excluded.keyword_count,
        error = excluded.error,
        params = excluded.params,
        data = excluded.data
"""

_ACTIVE_STATUSES = (RunStatus.PENDING.value, RunStatus.RUNNING.value)

# Seconds a statement waits for another process (e.g. CLI vs. server) to release a write lock.
_BUSY_TIMEOUT_SECONDS = 10.0


def _timestamp(value: datetime) -> float:
    """Epoch seconds; naive datetimes are taken as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _row_values(run: ResearchRun) -> tuple[object, ...]:
    keyword_count = len(run.results) if run.status is RunStatus.SUCCEEDED else run.progress.keywords
    return (
        run.id,
        _timestamp(run.created_at),
        None if run.finished_at is None else _timestamp(run.finished_at),
        run.status.value,
        run.params.seed,
        run.params.store.value,
        run.params.country,
        keyword_count,
        run.error,
        run.params.model_dump_json(),
        run.model_dump_json(),
    )


class RunRepository:
    """Persists :class:`ResearchRun` objects (full JSON plus indexed summary columns).

    One SQLite connection (WAL mode, so the CLI and the server can use the same file)
    is guarded by a lock; blocking work runs in worker threads via
    :func:`asyncio.to_thread`. Call :meth:`init` before anything else and :meth:`close`
    when done.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    async def init(self) -> None:
        """Open the database and create/migrate the schema. Safe to call more than once."""
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"run repository {self._path} is closed")
            if self._conn is not None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._path),
                timeout=_BUSY_TIMEOUT_SECONDS,
                isolation_level=None,  # autocommit; explicit BEGIN where atomicity matters
                check_same_thread=False,
            )
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._migrate(conn)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        logger.debug("run repository opened at %s", self._path)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        if version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"run database schema version {version} is newer than this program supports "
                f"({_SCHEMA_VERSION}); upgrade keyfinder"
            )
        if version < 1:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA_V1:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    async def save(self, run: ResearchRun) -> None:
        """Insert or replace ``run``."""
        values = _row_values(run)
        await self._run(lambda conn: conn.execute(_UPSERT, values))

    async def get(self, run_id: str) -> ResearchRun | None:
        """The stored run, or None if unknown (or its stored JSON is unreadable)."""
        row = await self._run(
            lambda conn: conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        )
        if row is None:
            return None
        try:
            return ResearchRun.model_validate_json(row[0])
        except ValidationError:
            logger.warning("stored run %s is unreadable; ignoring it", run_id, exc_info=True)
            return None

    async def list(self, limit: int = 50, offset: int = 0) -> list[RunSummary]:
        """Summaries of stored runs, newest first."""
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        rows = await self._run(
            lambda conn: conn.execute(
                "SELECT id, params, status, created_at, finished_at, keyword_count, error FROM runs "
                "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        )
        summaries: list[RunSummary] = []
        for run_id, params, status, created_at, finished_at, keyword_count, error in rows:
            try:
                summaries.append(
                    RunSummary(
                        id=run_id,
                        params=ResearchParams.model_validate_json(params),
                        status=RunStatus(status),
                        created_at=_from_timestamp(created_at),
                        finished_at=None if finished_at is None else _from_timestamp(finished_at),
                        keyword_count=keyword_count,
                        error=error,
                    )
                )
            except (ValidationError, ValueError):
                logger.warning("stored run %s is unreadable; skipping it", run_id, exc_info=True)
        return summaries

    async def delete(self, run_id: str) -> bool:
        """Remove a run; True if it existed."""
        removed = await self._run(
            lambda conn: conn.execute("DELETE FROM runs WHERE id = ?", (run_id,)).rowcount
        )
        return bool(removed)

    async def mark_interrupted(self) -> int:
        """Mark every PENDING/RUNNING run as FAILED ("interrupted by restart").

        Meant for startup, when no run can still be active. Returns the number of runs changed.
        """
        count = await self._run(self._mark_interrupted_sync)
        if count:
            logger.warning("marked %d unfinished run(s) as interrupted", count, extra={"runs": count})
        return count

    def _mark_interrupted_sync(self, conn: sqlite3.Connection) -> int:
        now = utcnow()
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, data FROM runs WHERE status IN (?, ?)", _ACTIVE_STATUSES
            ).fetchall()
            for run_id, data in rows:
                try:
                    run = ResearchRun.model_validate_json(data)
                except ValidationError:
                    logger.warning("stored run %s is unreadable; marking columns only", run_id, exc_info=True)
                    conn.execute(
                        "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                        (RunStatus.FAILED.value, INTERRUPTED_ERROR, _timestamp(now), run_id),
                    )
                    continue
                run.status = RunStatus.FAILED
                run.error = INTERRUPTED_ERROR
                run.finished_at = now
                conn.execute(_UPSERT, _row_values(run))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return len(rows)

    def close(self) -> None:
        """Close the connection (idempotent). Later operations raise ``RuntimeError``."""
        with self._lock:
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def _run(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return await asyncio.to_thread(self._locked, operation)

    def _locked(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._lock:
            if self._conn is None:
                state = "closed" if self._closed else "not initialised (call init() first)"
                raise RuntimeError(f"run repository {self._path} is {state}")
            return operation(self._conn)
