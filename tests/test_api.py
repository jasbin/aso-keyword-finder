from __future__ import annotations

import asyncio
import csv
import io
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from keyfinder import __version__
from keyfinder.api import SECURITY_HEADERS, create_app, export_filename
from keyfinder.cache import ResponseCache
from keyfinder.config import Settings
from keyfinder.http import HttpClient
from keyfinder.jobs import JobManager
from keyfinder.models import AppSummary, ResearchParams, ResearchRun, RunStatus, Store
from keyfinder.storage import INTERRUPTED_ERROR, RunRepository
from keyfinder.stores import STOREFRONTS, StoreClient, StoreUnavailableError

API_KEY = "s3cret-key"


class FakeStore:
    store = Store.APPSTORE
    supports_difficulty = True

    def __init__(self, store: Store, *, gate: asyncio.Event | None, error: Exception | None) -> None:
        self.store = store
        self.supports_difficulty = store is Store.APPSTORE
        self.gate = gate
        self.error = error
        self.started = asyncio.Event()

    async def suggest(self, term: str) -> list[str]:
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        if " " not in term:
            return [term, f"{term} editor", f"={term} formula"]
        return []

    async def top_apps(self, term: str, limit: int = 10) -> list[AppSummary] | None:
        return [AppSummary(app_id="1", title=f"{term} Pro", developer="Example", ratings=5000)]


class FakeFactory:
    """client_factory hook: builds FakeStores and remembers them."""

    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None
        self.stores: list[FakeStore] = []
        self.params: list[ResearchParams] = []

    def __call__(self, params: ResearchParams, http: HttpClient, cache: ResponseCache | None) -> StoreClient:
        self.params.append(params)
        store = FakeStore(params.store, gate=self.gate, error=self.error)
        self.stores.append(store)
        return store


Client = httpx.AsyncClient
MakeClient = Callable[..., Awaitable[Client]]


@pytest.fixture
def factory() -> FakeFactory:
    return FakeFactory()


@pytest.fixture
async def make_client(settings: Settings, factory: FakeFactory) -> AsyncIterator[MakeClient]:
    """Start the app (with lifespan) and return an httpx client bound to it."""
    stack: list[Any] = []

    async def make(**overrides: Any) -> Client:
        app = create_app(settings.model_copy(update=overrides), client_factory=factory)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        stack.append((lifespan, client, app))
        return client

    yield make
    for lifespan, client, _ in reversed(stack):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


@pytest.fixture
async def client(make_client: MakeClient) -> Client:
    return await make_client()


def app_of(client: Client) -> FastAPI:
    transport = client._transport
    assert isinstance(transport, httpx.ASGITransport)
    app = transport.app
    assert isinstance(app, FastAPI)
    return app


async def wait_status(client: Client, run_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    async def poll() -> dict[str, Any]:
        while True:
            response = await client.get(f"/api/runs/{run_id}")
            assert response.status_code == 200
            body: dict[str, Any] = response.json()
            if body["status"] in ("succeeded", "failed", "cancelled") and not jobs_of(client).is_active(
                run_id
            ):
                return body
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout)


def jobs_of(client: Client) -> JobManager:
    jobs = app_of(client).state.jobs
    assert isinstance(jobs, JobManager)
    return jobs


async def submit(client: Client, **body: Any) -> dict[str, Any]:
    payload = {"seed": "photo", "limit": 10, **body}
    response = await client.post("/api/runs", json=payload)
    assert response.status_code == 202, response.text
    assert response.headers["location"] == f"/api/runs/{response.json()['id']}"
    result: dict[str, Any] = response.json()
    return result


# ---------------------------------------------------------------------- basics


async def test_healthz_and_meta(client: Client) -> None:
    health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "version": __version__}

    meta = (await client.get("/api/meta")).json()
    assert meta["version"] == __version__
    assert meta["stores"] == ["appstore", "play"]
    assert meta["countries"] == sorted(STOREFRONTS)
    assert meta["limits"] == {"limit": [5, 300], "depth": [1, 2]}
    assert meta["auth_required"] is False


async def test_security_headers_everywhere(client: Client) -> None:
    for path in ("/healthz", "/api/meta", "/", "/api/runs/nope", "/does-not-exist"):
        response = await client.get(path)
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (path, name)
    assert "frame-ancestors 'none'" in SECURITY_HEADERS["Content-Security-Policy"]
    assert (await client.get("/api/meta")).headers["cache-control"] == "no-store"


async def test_index_served(client: Client) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in response.text.lower()
    static = await client.get("/static/index.html")
    assert static.status_code == 200
    missing = await client.get("/static/nope.js")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}


async def test_docs_disabled_but_openapi_available(client: Client) -> None:
    assert (await client.get("/docs")).status_code == 404
    assert (await client.get("/openapi.json")).status_code == 200


# ---------------------------------------------------------------------- runs


async def test_submit_poll_succeeds(client: Client, factory: FakeFactory) -> None:
    created = await submit(client, seed="  Photo ", country="GB", probe=False)
    assert created["status"] == "pending"
    assert created["params"]["seed"] == "photo"
    assert created["params"]["country"] == "gb"

    final = await wait_status(client, created["id"])
    assert final["status"] == "succeeded"
    assert final["error"] is None
    keywords = {r["keyword"] for r in final["results"]}
    assert keywords == {"photo", "photo editor", "=photo formula"}
    assert final["results"][0]["top_apps"][0]["title"].endswith("Pro")
    assert factory.params[0].country == "gb"

    listing = (await client.get("/api/runs")).json()
    assert [r["id"] for r in listing] == [created["id"]]
    assert listing[0]["keyword_count"] == 3
    assert "results" not in listing[0]


async def test_play_store_any_country(client: Client) -> None:
    created = await submit(client, store="play", country="zz", probe=False)
    final = await wait_status(client, created["id"])
    assert final["status"] == "succeeded"
    assert all(r["difficulty"] is None for r in final["results"])


async def test_store_failure_is_friendly(client: Client, factory: FakeFactory) -> None:
    factory.error = StoreUnavailableError("search.itunes.apple.com is unavailable (HTTP 403; 2 attempts)")
    created = await submit(client)
    final = await wait_status(client, created["id"])
    assert final["status"] == "failed"
    assert "could not be reached" in final["error"]
    assert "HTTP 403" in final["error"]
    assert final["results"] == []


async def test_validation_errors_422(client: Client) -> None:
    for body in (
        {"seed": ""},
        {"seed": "   "},
        {"seed": "x", "limit": 1000},
        {"seed": "x", "depth": 3},
        {"seed": "x", "country": "usa"},
        {"seed": "x", "store": "windows"},
        {},
    ):
        response = await client.post("/api/runs", json=body)
        assert response.status_code == 422, body
        assert isinstance(response.json()["detail"], list)
    bad_json = await client.post("/api/runs", content=b"{", headers={"content-type": "application/json"})
    assert bad_json.status_code == 422


async def test_unknown_appstore_country_400(client: Client, factory: FakeFactory) -> None:
    response = await client.post("/api/runs", json={"seed": "photo", "country": "zz"})
    assert response.status_code == 400
    assert "zz" in response.json()["detail"]
    assert factory.params == []


async def test_factory_value_error_400(client: Client, factory: FakeFactory) -> None:
    def failing(params: ResearchParams, http: HttpClient, cache: ResponseCache | None) -> StoreClient:
        raise ValueError("language not supported")

    jobs_of(client)._client_factory = failing
    response = await client.post("/api/runs", json={"seed": "photo"})
    assert response.status_code == 400
    assert response.json() == {"detail": "language not supported"}


async def test_get_unknown_run_404(client: Client) -> None:
    for method, path in (
        ("GET", "/api/runs/nope"),
        ("POST", "/api/runs/nope/cancel"),
        ("DELETE", "/api/runs/nope"),
        ("GET", "/api/runs/nope/export"),
    ):
        response = await client.request(method, path)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "run not found"}


async def test_cancel_run(client: Client, factory: FakeFactory) -> None:
    factory.gate = asyncio.Event()
    created = await submit(client)
    await asyncio.wait_for(_first_started(factory), 5)

    response = await client.post(f"/api/runs/{created['id']}/cancel")
    assert response.status_code == 200
    assert response.json() == {"cancelled": True}
    final = await wait_status(client, created["id"])
    assert final["status"] == "cancelled"

    again = await client.post(f"/api/runs/{created['id']}/cancel")
    assert again.json() == {"cancelled": False}


async def test_concurrency_limit_queues_runs(make_client: MakeClient, factory: FakeFactory) -> None:
    client = await make_client(max_concurrent_runs=1)
    factory.gate = asyncio.Event()
    first = await submit(client)
    second = await submit(client, seed="video")
    await asyncio.wait_for(_first_started(factory), 5)
    await asyncio.sleep(0.05)

    assert (await client.get(f"/api/runs/{first['id']}")).json()["status"] == "running"
    queued = (await client.get(f"/api/runs/{second['id']}")).json()
    assert queued["status"] == "pending"
    assert queued["progress"]["stage"] == "queued"
    assert jobs_of(client).active_count == 2

    factory.gate.set()
    assert (await wait_status(client, first["id"]))["status"] == "succeeded"
    assert (await wait_status(client, second["id"]))["status"] == "succeeded"


async def test_history_list_and_delete(client: Client, factory: FakeFactory) -> None:
    ids = []
    for seed in ("alpha", "beta", "gamma"):
        created = await submit(client, seed=seed, probe=False)
        await wait_status(client, created["id"])
        ids.append(created["id"])

    listing = (await client.get("/api/runs")).json()
    assert [r["id"] for r in listing] == ids[::-1]
    page = (await client.get("/api/runs", params={"limit": 1, "offset": 1})).json()
    assert [r["id"] for r in page] == [ids[1]]
    assert (await client.get("/api/runs", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/runs", params={"offset": -1})).status_code == 422

    response = await client.delete(f"/api/runs/{ids[0]}")
    assert response.status_code == 204
    assert response.content == b""
    assert (await client.get(f"/api/runs/{ids[0]}")).status_code == 404
    assert (await client.delete(f"/api/runs/{ids[0]}")).status_code == 404


async def test_delete_active_run_409(client: Client, factory: FakeFactory) -> None:
    factory.gate = asyncio.Event()
    created = await submit(client)
    response = await client.delete(f"/api/runs/{created['id']}")
    assert response.status_code == 409
    assert "cancel" in response.json()["detail"]
    factory.gate.set()
    await wait_status(client, created["id"])
    assert (await client.delete(f"/api/runs/{created['id']}")).status_code == 204


# ---------------------------------------------------------------------- export


async def test_export_csv_and_json(client: Client) -> None:
    created = await submit(client, seed="Photo!", probe=False)
    run_id = created["id"]
    await wait_status(client, run_id)

    csv_response = await client.get(f"/api/runs/{run_id}/export", params={"format": "csv"})
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"] == "text/csv; charset=utf-8"
    assert csv_response.headers["content-disposition"] == (
        f'attachment; filename="keywords-photo-{run_id[:8]}.csv"'
    )
    rows = list(csv.reader(io.StringIO(csv_response.text)))
    assert rows[0] == [
        "keyword",
        "popularity",
        "difficulty",
        "opportunity",
        "chars_typed",
        "position",
        "top_apps",
    ]
    keywords = {row[0] for row in rows[1:]}
    assert keywords == {"photo!", "photo! editor", "'=photo! formula"}  # formula injection guarded

    default = await client.get(f"/api/runs/{run_id}/export")
    assert default.headers["content-type"] == "text/csv; charset=utf-8"

    json_response = await client.get(f"/api/runs/{run_id}/export", params={"format": "json"})
    assert json_response.status_code == 200
    assert json_response.headers["content-type"] == "application/json"
    assert json_response.headers["content-disposition"].endswith(f'-{run_id[:8]}.json"')
    body = json.loads(json_response.text)
    assert body["id"] == run_id
    assert body["status"] == "succeeded"
    assert len(body["results"]) == len(rows) - 1

    assert (await client.get(f"/api/runs/{run_id}/export", params={"format": "xml"})).status_code == 422


async def test_export_not_succeeded_409(client: Client, factory: FakeFactory) -> None:
    factory.gate = asyncio.Event()
    created = await submit(client)
    response = await client.get(f"/api/runs/{created['id']}/export")
    assert response.status_code == 409
    factory.gate.set()
    await wait_status(client, created["id"])


def test_export_filename_slug() -> None:
    run = ResearchRun(id="abcdef0123456789", params=ResearchParams(seed="Café & Photo — Editor"))
    assert export_filename(run, "csv") == "keywords-caf-photo-editor-abcdef01.csv"
    unicode_only = ResearchRun(id="0123456789", params=ResearchParams(seed="写真"))
    assert export_filename(unicode_only, "json") == "keywords-run-01234567.json"
    long = ResearchRun(id="0123456789", params=ResearchParams(seed="a " * 50))
    assert len(export_filename(long, "csv")) <= len("keywords--01234567.csv") + 40


# ---------------------------------------------------------------------- auth


@pytest.mark.parametrize("configured", [None, ""])
async def test_auth_off_when_key_unset_or_empty(make_client: MakeClient, configured: str | None) -> None:
    client = await make_client(api_key=None if configured is None else SecretStr(configured))
    assert (await client.get("/api/runs")).status_code == 200
    assert (await client.get("/api/meta")).json()["auth_required"] is False


async def test_api_key_auth(make_client: MakeClient) -> None:
    client = await make_client(api_key=SecretStr(API_KEY))

    assert (await client.get("/healthz")).status_code == 200
    meta = await client.get("/api/meta")
    assert meta.status_code == 200
    assert meta.json()["auth_required"] is True
    assert (await client.get("/")).status_code == 200

    for headers in (
        {},
        {"X-API-Key": "wrong"},
        {"Authorization": "Bearer wrong"},
        {"Authorization": API_KEY},
    ):
        response = await client.get("/api/runs", headers=headers)
        assert response.status_code == 401, headers
        assert response.json() == {"detail": "invalid or missing API key"}
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["x-content-type-options"] == "nosniff"

    unauthenticated_post = await client.post("/api/runs", json={"seed": "photo"})
    assert unauthenticated_post.status_code == 401
    assert (await client.get("/api/runs/x", headers={b"X-API-Key": "ünïcode".encode()})).status_code == 401

    assert (await client.get("/api/runs", headers={"X-API-Key": API_KEY})).status_code == 200
    assert (await client.get("/api/runs", headers={"Authorization": f"Bearer {API_KEY}"})).status_code == 200
    assert (await client.get("/api/runs", headers={"Authorization": f"bearer {API_KEY}"})).status_code == 200
    created = await client.post("/api/runs", json={"seed": "photo"}, headers={"X-API-Key": API_KEY})
    assert created.status_code == 202


# ---------------------------------------------------------------------- lifecycle


async def test_startup_marks_interrupted_runs(settings: Settings, factory: FakeFactory) -> None:
    settings.ensure_dirs()
    repo = RunRepository(settings.db_path)
    await repo.init()
    stale = ResearchRun(id="stale-run", params=ResearchParams(seed="photo"), status=RunStatus.RUNNING)
    await repo.save(stale)
    repo.close()

    app = create_app(settings, client_factory=factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            body = (await client.get("/api/runs/stale-run")).json()
    assert body["status"] == "failed"
    assert body["error"] == INTERRUPTED_ERROR


async def test_shutdown_cancels_active_runs(settings: Settings, factory: FakeFactory) -> None:
    factory.gate = asyncio.Event()
    app = create_app(settings, client_factory=factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            created = await submit(client)
            await asyncio.wait_for(_first_started(factory), 5)

    repo = RunRepository(settings.db_path)
    await repo.init()
    try:
        stored = await repo.get(created["id"])
    finally:
        repo.close()
    assert stored is not None
    assert stored.status is RunStatus.CANCELLED


def test_sync_testclient_lifespan(settings: Settings, factory: FakeFactory) -> None:
    app = create_app(settings.model_copy(update={"cache_ttl_seconds": 0}), client_factory=factory)
    with TestClient(app) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        created = client.post("/api/runs", json={"seed": "photo", "probe": False})
        assert created.status_code == 202
    assert settings.db_path.exists()


async def test_cors_only_when_configured(make_client: MakeClient) -> None:
    plain = await make_client()
    response = await plain.get("/api/meta", headers={"Origin": "https://example.com"})
    assert "access-control-allow-origin" not in response.headers

    cors = await make_client(cors_origins=["https://example.com"])
    response = await cors.get("/api/meta", headers={"Origin": "https://example.com"})
    assert response.headers["access-control-allow-origin"] == "https://example.com"


async def test_unhandled_error_returns_json_500(settings: Settings, factory: FakeFactory) -> None:
    app = create_app(settings, client_factory=factory)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}


async def _first_started(factory: FakeFactory) -> None:
    # The client factory runs inside submit(), so the first store exists once submit returned.
    await factory.stores[0].started.wait()
