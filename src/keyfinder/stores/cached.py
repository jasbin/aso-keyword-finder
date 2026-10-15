"""Caching decorator for StoreClient implementations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

from keyfinder.models import AppSummary
from keyfinder.stores.base import StoreClient

if TYPE_CHECKING:
    from keyfinder.cache import ResponseCache

logger = logging.getLogger(__name__)

_APP_LIST = TypeAdapter(list[AppSummary])


def _warn_malformed(namespace: str, key: str) -> None:
    logger.warning("ignoring malformed cache entry %s/%r; refetching", namespace, key)


class CachedStoreClient:
    """Serve `suggest` / `top_apps` from a ResponseCache, falling back to the wrapped client.

    Only successful answers are stored: errors propagate and are never cached, and a ``None``
    from ``top_apps`` (store has no rankings) is not cached either. The cache is best-effort:
    a failing cache backend or a malformed entry is logged and treated as a miss, never as an
    error. ``hits`` / ``misses`` count lookups, for progress reporting.
    """

    def __init__(self, inner: StoreClient, cache: ResponseCache, namespace: str) -> None:
        self._inner = inner
        self._cache = cache
        self._suggest_ns = f"{namespace}:suggest"
        self._top_ns = f"{namespace}:top"
        self.store = inner.store
        self.supports_difficulty = inner.supports_difficulty
        self.hits = 0
        self.misses = 0

    async def suggest(self, term: str) -> list[str]:
        cached = await self._get(self._suggest_ns, term)
        if cached is not None:
            if isinstance(cached, list) and all(isinstance(t, str) for t in cached):
                self.hits += 1
                return cached
            _warn_malformed(self._suggest_ns, term)
        self.misses += 1
        terms = await self._inner.suggest(term)
        await self._set(self._suggest_ns, term, terms)
        return terms

    async def top_apps(self, term: str, limit: int = 10) -> list[AppSummary] | None:
        key = f"{limit}:{term}"
        cached = await self._get(self._top_ns, key)
        if cached is not None:
            try:
                apps = _APP_LIST.validate_python(cached)
            except ValidationError:
                _warn_malformed(self._top_ns, key)
            else:
                self.hits += 1
                return apps
        self.misses += 1
        result = await self._inner.top_apps(term, limit)
        if result is not None:
            await self._set(self._top_ns, key, [app.model_dump(mode="json") for app in result])
        return result

    async def _get(self, namespace: str, key: str) -> Any:
        try:
            return await self._cache.get(namespace, key)
        except Exception as exc:  # the cache is an optimisation: never fail a lookup because of it
            logger.warning("response cache read failed for %s: %s", namespace, exc)
            return None

    async def _set(self, namespace: str, key: str, value: Any) -> None:
        try:
            await self._cache.set(namespace, key, value)
        except Exception as exc:  # see _get
            logger.warning("response cache write failed for %s: %s", namespace, exc)
