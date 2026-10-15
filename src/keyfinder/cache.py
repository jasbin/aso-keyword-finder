"""SQLite-backed TTL cache for store responses (JSON values), safe to share between tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

__all__ = ["ResponseCache"]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS cache (
        namespace  TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (namespace, key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS cache_expires_at ON cache (expires_at)",
)

# Seconds a statement waits for another process (e.g. CLI vs. server) to release a write lock.
_BUSY_TIMEOUT_SECONDS = 10.0


class ResponseCache:
    """Key/value cache with a fixed time-to-live, persisted in SQLite.

    Values are stored as JSON, so anything ``json.dumps`` accepts can be cached and
    comes back as plain JSON types. Expiry uses the wall clock (``time.time()``), so it
    survives restarts. ``ttl_seconds == 0`` disables caching: :meth:`get` always misses
    and :meth:`set` is a no-op, while :meth:`purge_expired`, :meth:`clear` and
    :meth:`stats` still operate on whatever the file already holds.

    One connection serves all callers: a lock serialises access and blocking SQLite
    work runs in worker threads via :func:`asyncio.to_thread`.
    """

    def __init__(self, path: Path, ttl_seconds: int) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be >= 0 (0 disables the cache)")
        self._path = path
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._closed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path),
            timeout=_BUSY_TIMEOUT_SECONDS,
            isolation_level=None,  # autocommit: every statement is its own transaction
            check_same_thread=False,
        )
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            for statement in _SCHEMA:
                self._conn.execute(statement)
        except sqlite3.Error:
            self._conn.close()
            raise
        logger.debug("response cache opened at %s (ttl=%ss)", path, ttl_seconds, extra={"ttl": ttl_seconds})

    @property
    def path(self) -> Path:
        return self._path

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    async def get(self, namespace: str, key: str) -> Any | None:
        """The cached JSON-decoded value, or None when missing, expired or disabled."""
        if not self.enabled:
            return None
        row = await self._run(
            lambda conn: conn.execute(
                "SELECT value FROM cache WHERE namespace = ? AND key = ? AND expires_at > ?",
                (namespace, key, time.time()),
            ).fetchone()
        )
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            logger.warning(
                "ignoring undecodable cache entry %s/%s", namespace, key, extra={"namespace": namespace}
            )
            return None

    async def set(self, namespace: str, key: str, value: Any) -> None:
        """Store ``value`` (must be JSON-serialisable, else ``TypeError``/``ValueError``)."""
        if not self.enabled:
            return
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        expires_at = time.time() + self._ttl
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO cache (namespace, key, value, expires_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (namespace, key) DO UPDATE SET "
                "value = excluded.value, expires_at = excluded.expires_at",
                (namespace, key, encoded, expires_at),
            )
        )

    async def purge_expired(self) -> int:
        """Delete expired entries; returns how many were removed."""
        removed = await self._run(
            lambda conn: conn.execute("DELETE FROM cache WHERE expires_at <= ?", (time.time(),)).rowcount
        )
        if removed:
            logger.info("purged %d expired cache entries", removed, extra={"removed": removed})
        return removed

    async def clear(self) -> int:
        """Delete every entry; returns how many were removed."""
        removed = await self._run(lambda conn: conn.execute("DELETE FROM cache").rowcount)
        logger.info("cleared %d cache entries", removed, extra={"removed": removed})
        return removed

    async def stats(self) -> dict[str, int]:
        """``{"entries": rows stored (incl. expired), "expired": rows past their TTL}``."""

        def query(conn: sqlite3.Connection) -> tuple[int, int]:
            entries, expired = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(expires_at <= ?), 0) FROM cache", (time.time(),)
            ).fetchone()
            return int(entries), int(expired)

        entries, expired = await self._run(query)
        return {"entries": entries, "expired": expired}

    def close(self) -> None:
        """Close the database connection (idempotent). Later operations raise ``RuntimeError``."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    async def _run(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return await asyncio.to_thread(self._locked, operation)

    def _locked(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"response cache {self._path} is closed")
            return operation(self._conn)
