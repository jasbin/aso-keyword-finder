from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import keyfinder.stores as stores
from keyfinder.models import AppSummary, ResearchParams, Store
from keyfinder.stores import (
    STOREFRONTS,
    AppStoreClient,
    CachedStoreClient,
    GooglePlayClient,
    StoreClient,
    StoreError,
    StoreParseError,
    StoreRateLimitedError,
    StoreUnavailableError,
    create_store_client,
)

FIXTURES = Path(__file__).parent / "fixtures"
NS = "appstore:us:en"

APPS = [
    AppSummary(app_id="1", title="Stillwater: Meditation", developer="Stillwater", ratings=1500, rating=4.8),
    AppSummary(app_id="2", title="Zen Timer", ratings=0, url="https://apps.apple.com/us/app/zen-timer/id2"),
]


class FakeCache:
    """In-memory stand-in for keyfinder.cache.ResponseCache, with the same JSON round-trip."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    async def get(self, namespace: str, key: str) -> Any | None:
        raw = self.data.get((namespace, key))
        return None if raw is None else json.loads(raw)

    async def set(self, namespace: str, key: str, value: Any) -> None:
        self.data[(namespace, key)] = json.dumps(value)

    def stored(self, namespace: str, key: str) -> Any:
        return json.loads(self.data[(namespace, key)])


class BrokenCache:
    """A cache whose backend fails on every operation."""

    async def get(self, namespace: str, key: str) -> Any | None:
        raise sqlite3.OperationalError("database is locked")

    async def set(self, namespace: str, key: str, value: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")


class FakeStore:
    """StoreClient double that counts calls and returns (or raises) what each test configures."""

    def __init__(self, store: Store = Store.APPSTORE, supports_difficulty: bool = True) -> None:
        self.store = store
        self.supports_difficulty = supports_difficulty
        self.suggestions: list[str] | Exception = ["meditation", "meditation app"]
        self.apps: list[AppSummary] | Exception | None = list(APPS)
        self.suggest_calls: list[str] = []
        self.top_calls: list[tuple[str, int]] = []

    async def suggest(self, term: str) -> list[str]:
        self.suggest_calls.append(term)
        if isinstance(self.suggestions, Exception):
            raise self.suggestions
        return list(self.suggestions)

    async def top_apps(self, term: str, limit: int = 10) -> list[AppSummary] | None:
        self.top_calls.append((term, limit))
        if isinstance(self.apps, Exception):
            raise self.apps
        return None if self.apps is None else list(self.apps)


def make(inner: FakeStore | None = None, cache: Any = None) -> tuple[CachedStoreClient, FakeStore, Any]:
    inner = inner or FakeStore()
    cache = FakeCache() if cache is None else cache
    return CachedStoreClient(inner, cache, NS), inner, cache


# --------------------------------------------------------------------------- CachedStoreClient


@pytest.mark.parametrize(("store", "difficulty"), [(Store.APPSTORE, True), (Store.PLAY, False)])
def test_proxies_store_attributes(store: Store, difficulty: bool) -> None:
    client, _, _ = make(FakeStore(store, difficulty))
    assert isinstance(client, StoreClient)
    assert client.store is store
    assert client.supports_difficulty is difficulty
    assert (client.hits, client.misses) == (0, 0)


async def test_suggest_miss_then_hit() -> None:
    client, inner, cache = make()

    assert await client.suggest("meditation") == ["meditation", "meditation app"]
    assert (client.hits, client.misses) == (0, 1)
    assert cache.stored(f"{NS}:suggest", "meditation") == ["meditation", "meditation app"]

    assert await client.suggest("meditation") == ["meditation", "meditation app"]
    assert (client.hits, client.misses) == (1, 1)
    assert inner.suggest_calls == ["meditation"]


async def test_suggest_terms_are_cached_separately() -> None:
    client, inner, _ = make()
    await client.suggest("meditation")
    await client.suggest("meditation a")
    await client.suggest("meditation")
    assert inner.suggest_calls == ["meditation", "meditation a"]
    assert (client.hits, client.misses) == (1, 2)


async def test_empty_suggestion_list_is_cached() -> None:
    client, inner, cache = make()
    inner.suggestions = []
    assert await client.suggest("qzx") == []
    assert await client.suggest("qzx") == []
    assert inner.suggest_calls == ["qzx"]
    assert cache.stored(f"{NS}:suggest", "qzx") == []
    assert client.hits == 1


async def test_top_apps_cached_as_dicts_keyed_by_limit_and_term() -> None:
    client, inner, cache = make()

    first = await client.top_apps("meditation")
    assert first == APPS
    assert cache.stored(f"{NS}:top", "10:meditation") == [app.model_dump(mode="json") for app in APPS]

    second = await client.top_apps("meditation")
    assert second == APPS
    assert all(isinstance(app, AppSummary) for app in second or [])
    assert inner.top_calls == [("meditation", 10)]
    assert (client.hits, client.misses) == (1, 1)

    await client.top_apps("meditation", limit=5)
    assert inner.top_calls == [("meditation", 10), ("meditation", 5)]
    assert (f"{NS}:top", "5:meditation") in cache.data


async def test_none_top_apps_is_not_cached() -> None:
    client, inner, cache = make(FakeStore(Store.PLAY, False))
    inner.apps = None
    assert await client.top_apps("photo editor") is None
    assert await client.top_apps("photo editor") is None
    assert len(inner.top_calls) == 2
    assert cache.data == {}
    assert (client.hits, client.misses) == (0, 2)


@pytest.mark.parametrize(
    "error",
    [StoreUnavailableError("down"), StoreRateLimitedError("slow down"), StoreParseError("changed")],
)
async def test_errors_propagate_and_are_never_cached(error: StoreError) -> None:
    client, inner, cache = make()
    inner.suggestions = error
    inner.apps = error

    with pytest.raises(type(error)):
        await client.suggest("meditation")
    with pytest.raises(type(error)):
        await client.top_apps("meditation")
    assert cache.data == {}

    inner.suggestions = ["meditation"]
    inner.apps = list(APPS)
    assert await client.suggest("meditation") == ["meditation"]
    assert await client.top_apps("meditation") == APPS
    assert len(cache.data) == 2


@pytest.mark.parametrize(
    "stored",
    [{"not": "a list"}, "meditation", [1, 2], ["ok", None]],
    ids=["dict", "string", "ints", "mixed"],
)
async def test_malformed_suggest_entry_is_refetched(stored: Any, caplog: pytest.LogCaptureFixture) -> None:
    client, inner, cache = make()
    await cache.set(f"{NS}:suggest", "meditation", stored)

    with caplog.at_level(logging.WARNING, logger="keyfinder.stores.cached"):
        assert await client.suggest("meditation") == ["meditation", "meditation app"]

    assert inner.suggest_calls == ["meditation"]
    assert cache.stored(f"{NS}:suggest", "meditation") == ["meditation", "meditation app"]
    assert (client.hits, client.misses) == (0, 1)
    assert "malformed cache entry" in caplog.text


@pytest.mark.parametrize(
    "stored",
    [{"app_id": "1"}, [{"title": "missing id"}], [{"app_id": "1", "title": "x", "ratings": -5}], "apps"],
    ids=["dict", "missing-field", "invalid-field", "string"],
)
async def test_malformed_top_apps_entry_is_refetched(stored: Any, caplog: pytest.LogCaptureFixture) -> None:
    client, inner, cache = make()
    await cache.set(f"{NS}:top", "10:meditation", stored)

    with caplog.at_level(logging.WARNING, logger="keyfinder.stores.cached"):
        assert await client.top_apps("meditation") == APPS

    assert inner.top_calls == [("meditation", 10)]
    assert cache.stored(f"{NS}:top", "10:meditation") == [app.model_dump(mode="json") for app in APPS]
    assert "malformed cache entry" in caplog.text


async def test_failing_cache_backend_is_bypassed(caplog: pytest.LogCaptureFixture) -> None:
    client, inner, _ = make(cache=BrokenCache())

    with caplog.at_level(logging.WARNING, logger="keyfinder.stores.cached"):
        assert await client.suggest("meditation") == ["meditation", "meditation app"]
        assert await client.top_apps("meditation") == APPS

    assert inner.suggest_calls == ["meditation"]
    assert inner.top_calls == [("meditation", 10)]
    assert (client.hits, client.misses) == (0, 2)
    assert "response cache read failed" in caplog.text
    assert "response cache write failed" in caplog.text


async def test_cancellation_is_not_swallowed() -> None:
    class CancellingCache(FakeCache):
        async def get(self, namespace: str, key: str) -> Any | None:
            raise asyncio.CancelledError

    client, inner, _ = make(cache=CancellingCache())
    with pytest.raises(asyncio.CancelledError):
        await client.suggest("meditation")
    assert inner.suggest_calls == []


# --------------------------------------------------------------------------- create_store_client


class FakeHttp:
    """Stand-in for keyfinder.http.HttpClient returning one canned body for every request."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def get(
        self, url: str, *, params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None
    ) -> bytes:
        return self.body

    async def post(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        return self.body


def test_factory_builds_app_store_client() -> None:
    params = ResearchParams(seed="meditation", store=Store.APPSTORE, country="GB", language="en-gb")
    client = create_store_client(params, FakeHttp(b""))  # type: ignore[arg-type]
    assert isinstance(client, AppStoreClient)
    assert client.country == "gb"
    assert client.storefront == "143444-1,29"


def test_factory_builds_google_play_client() -> None:
    params = ResearchParams(seed="photo editor", store=Store.PLAY, country="np", language="ne")
    client = create_store_client(params, FakeHttp(b""))  # type: ignore[arg-type]
    assert isinstance(client, GooglePlayClient)
    assert (client.country, client.language) == ("np", "ne")


def test_factory_rejects_unknown_app_store_country() -> None:
    params = ResearchParams(seed="meditation", store=Store.APPSTORE, country="zz")
    with pytest.raises(ValueError, match="unknown App Store country"):
        create_store_client(params, FakeHttp(b""))  # type: ignore[arg-type]


async def test_factory_wraps_with_cache_using_store_country_language_namespace() -> None:
    hints = (FIXTURES / "appstore_hints.xml").read_bytes()
    cache = FakeCache()
    params = ResearchParams(seed="meditation", store=Store.APPSTORE, country="de", language="de")
    client = create_store_client(params, FakeHttp(hints), cache)  # type: ignore[arg-type]

    assert isinstance(client, CachedStoreClient)
    assert client.store is Store.APPSTORE
    assert client.supports_difficulty is True
    terms = await client.suggest("meditation")
    assert terms[:2] == ["meditation", "meditation app"]
    assert list(cache.data) == [("appstore:de:de:suggest", "meditation")]


async def test_factory_namespace_for_google_play() -> None:
    chunked = (FIXTURES / "play_suggest_chunked.txt").read_bytes()
    cache = FakeCache()
    params = ResearchParams(seed="photo editor", store=Store.PLAY, country="in", language="hi")
    client = create_store_client(params, FakeHttp(chunked), cache)  # type: ignore[arg-type]

    assert isinstance(client, CachedStoreClient)
    assert client.supports_difficulty is False
    await client.suggest("photo editor")
    assert list(cache.data) == [("play:in:hi:suggest", "photo editor")]


def test_package_reexports() -> None:
    assert set(stores.__all__) == {
        "STOREFRONTS",
        "AppStoreClient",
        "CachedStoreClient",
        "GooglePlayClient",
        "StoreClient",
        "StoreClientFactory",
        "StoreError",
        "StoreParseError",
        "StoreRateLimitedError",
        "StoreUnavailableError",
        "create_store_client",
    }
    assert all(hasattr(stores, name) for name in stores.__all__)
    assert STOREFRONTS["us"] == 143441
    assert issubclass(StoreRateLimitedError, StoreUnavailableError)
    factory: stores.StoreClientFactory = create_store_client
    assert callable(factory)
