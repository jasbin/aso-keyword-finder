"""Polite asynchronous HTTP client shared by the store clients.

* one pooled :class:`httpx.AsyncClient` (timeout, User-Agent, redirects),
* a global cap on in-flight requests (``settings.max_concurrency``),
* per-host spacing of request *starts* (``settings.request_interval``),
* retries with exponential backoff + jitter on transport errors, 429 and 5xx,
  honouring ``Retry-After`` (capped),
* request statistics for progress reporting.

Failures surface as :class:`~keyfinder.stores.base.StoreUnavailableError` /
:class:`~keyfinder.stores.base.StoreRateLimitedError`. Error messages and log lines
name the host (and path), never the query string, which carries user search terms.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import math
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timezone
from types import TracebackType
from typing import TYPE_CHECKING

import httpx

from keyfinder.config import Settings

if TYPE_CHECKING:
    from keyfinder.stores.base import StoreUnavailableError

__all__ = ["HttpClient", "RequestStats"]

logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0
BACKOFF_JITTER_SECONDS = 0.25
RETRY_AFTER_CAP_SECONDS = 60.0

_MAX_DETAIL_CHARS = 200
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#'\"<>]*)\?[^\s#'\"<>]*")


@dataclass
class RequestStats:
    """Cumulative counters for the lifetime of an :class:`HttpClient`."""

    requests: int = 0  # attempts sent (incl. retries)
    retries: int = 0
    failures: int = 0  # calls that ultimately raised


def _backoff_delay(attempt: int) -> float:
    """Sleep before retry ``attempt + 1`` (``attempt`` is the 0-based index of the failed try)."""
    base = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt)
    # Jitter only de-synchronises clients; it has no security purpose.
    return base + random.uniform(0, BACKOFF_JITTER_SECONDS)  # noqa: S311


def _parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as seconds (delta-seconds or HTTP-date), clamped to ``[0, 60]``; None if absent/bad."""
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = when.timestamp() - time.time()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), RETRY_AFTER_CAP_SECONDS)


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _describe(exc: Exception) -> str:
    """Exception class and message with any URL query strings removed."""
    detail = _URL_QUERY_RE.sub(r"\1", str(exc)).strip()
    if len(detail) > _MAX_DETAIL_CHARS:
        detail = detail[: _MAX_DETAIL_CHARS - 1] + "…"
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


def _attempts(count: int) -> str:
    return f"{count} attempt" if count == 1 else f"{count} attempts"


def _store_error(message: str, *, rate_limited: bool = False) -> StoreUnavailableError:
    # Imported lazily: keyfinder.stores imports this module (for HttpClient), so a
    # module-level import here would be circular whenever keyfinder.http is imported first.
    from keyfinder.stores.base import StoreRateLimitedError, StoreUnavailableError

    return StoreRateLimitedError(message) if rate_limited else StoreUnavailableError(message)


class HttpClient:
    """Shared async HTTP client with concurrency limits, per-host politeness and retries.

    Use it from a single event loop and close it with :meth:`aclose` (or ``async with``).
    """

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._max_retries = settings.max_retries
        self._interval = settings.request_interval
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            transport=transport,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_start: dict[str, float] = {}
        self.stats = RequestStats()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close pooled connections. Further requests raise ``RuntimeError``."""
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """GET ``url`` and return the response body of a 2xx answer."""
        return await self._request("GET", url, params=params, data=None, headers=headers)

    async def post(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """POST ``data`` form-encoded to ``url`` and return the response body of a 2xx answer."""
        return await self._request("POST", url, params=params, data=data, headers=headers)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
    ) -> bytes:
        target = httpx.URL(url)
        host = target.host or "unknown host"
        path = target.path
        attempt = 0
        while True:
            try:
                response = await self._send_once(
                    method, url, host, path, params=params, data=data, headers=headers
                )
            except httpx.TransportError as exc:  # connect/read errors, timeouts, proxy refusals
                reason = _describe(exc)
                if attempt >= self._max_retries:
                    raise self._failed(
                        _store_error(f"{host} is unreachable ({reason}; {_attempts(attempt + 1)})"),
                        method,
                        host,
                        path,
                    ) from exc
                delay = _backoff_delay(attempt)
            except httpx.RequestError as exc:  # too many redirects, undecodable body: retrying won't help
                raise self._failed(
                    _store_error(f"request to {host} failed ({_describe(exc)})"), method, host, path
                ) from exc
            else:
                status = response.status_code
                if response.is_success:
                    return response.content
                if not _is_retryable_status(status):
                    raise self._failed(
                        _store_error(f"{host} rejected the request (HTTP {status} {response.reason_phrase})"),
                        method,
                        host,
                        path,
                    )
                if attempt >= self._max_retries:
                    if status == 429:
                        error = _store_error(
                            f"{host} is rate limiting requests (HTTP 429; {_attempts(attempt + 1)}); "
                            "try again later",
                            rate_limited=True,
                        )
                    else:
                        error = _store_error(
                            f"{host} is unavailable (HTTP {status}; {_attempts(attempt + 1)})"
                        )
                    raise self._failed(error, method, host, path)
                reason = f"HTTP {status}"
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else _backoff_delay(attempt)

            attempt += 1
            self.stats.retries += 1
            logger.info(
                "retrying %s %s%s in %.2fs (%s; retry %d/%d)",
                method,
                host,
                path,
                delay,
                reason,
                attempt,
                self._max_retries,
                extra={"host": host, "path": path, "attempt": attempt, "delay": round(delay, 3)},
            )
            # Sleep without holding a concurrency slot so other requests keep flowing.
            await asyncio.sleep(delay)

    async def _send_once(
        self,
        method: str,
        url: str,
        host: str,
        path: str,
        *,
        params: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        """One attempt: wait for the host's turn and a global slot, send, read the body."""
        await self._acquire_slot(host)
        try:
            self.stats.requests += 1
            started = time.monotonic()
            response = await self._client.request(method, url, params=params, data=data, headers=headers)
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.debug(
                "%s %s%s -> %d (%.0f ms)",
                method,
                host,
                path,
                response.status_code,
                elapsed_ms,
                extra={
                    "method": method,
                    "host": host,
                    "path": path,
                    "status": response.status_code,
                    "elapsed_ms": round(elapsed_ms),
                },
            )
            return response
        finally:
            self._semaphore.release()

    async def _acquire_slot(self, host: str) -> None:
        """Wait until ``host`` may receive another request, then take a global slot.

        The per-host lock is held while waiting for the slot (so same-host requests stay
        ordered and spaced) but the slot is not held while waiting for the host's turn,
        so a throttled host never blocks requests to other hosts. On return the caller
        owns one semaphore slot and must release it.
        """
        lock = self._host_locks.get(host)
        if lock is None:
            lock = self._host_locks[host] = asyncio.Lock()
        async with lock:
            last = self._last_start.get(host)
            if self._interval > 0 and last is not None:
                wait = last + self._interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            await self._semaphore.acquire()
            self._last_start[host] = time.monotonic()

    def _failed(
        self, error: StoreUnavailableError, method: str, host: str, path: str
    ) -> StoreUnavailableError:
        self.stats.failures += 1
        logger.warning(
            "%s %s%s failed: %s",
            method,
            host,
            path,
            error,
            extra={"host": host, "path": path, "error_type": type(error).__name__},
        )
        return error
