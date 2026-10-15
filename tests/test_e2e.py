"""End-to-end tests: the REAL HttpClient, store clients, ResponseCache, engine, storage, CLI and API.

Only the network is fake. respx intercepts the three store endpoints and answers from a small
deterministic "store universe", in the exact wire formats of the recorded fixtures in
tests/fixtures (the seed queries are answered with the fixture bytes themselves). Because the
universe is known, the expected chars_typed / position of every keyword can be computed
independently ("oracle") and compared with what the whole pipeline produced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import plistlib
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from typer.testing import CliRunner

from keyfinder import cli
from keyfinder import http as http_module
from keyfinder.api import create_app
from keyfinder.config import Settings, get_settings
from keyfinder.models import KeywordResult, ResearchRun, RunStatus
from keyfinder.scoring import difficulty_score, opportunity_score, popularity_score
from keyfinder.storage import RunRepository
from keyfinder.stores.appstore import HINTS_URL, SEARCH_URL, parse_hints, parse_search
from keyfinder.stores.googleplay import SUGGEST_URL, parse_suggest

FIXTURES = Path(__file__).parent / "fixtures"
HINTS_FIXTURE = (FIXTURES / "appstore_hints.xml").read_bytes()
SEARCH_FIXTURE = (FIXTURES / "itunes_search.json").read_bytes()
PLAY_FIXTURE = (FIXTURES / "play_suggest_single.txt").read_bytes()

# ----------------------------------------------------------------------------- store universes
# Terms in popularity order. A store's autocomplete for query q is the first N terms that start
# with q. The fixture lists must be exactly what the universe yields for the seeds (checked below).

APPSTORE_SEED = "meditation"
APPSTORE_UNIVERSE = [
    "maps",
    "messenger",
    "meditation",
    "meditation app",
    "meditation for sleep",
    "meditation music",
    "meditation timer",
    "meditation for beginners",
    "meditation for anxiety",
    "meditation sounds",
    "meditation for kids",
    "meditation bell",
    "medium",
    "meditation app free",  # only reachable through the a..z expansion (11th for "meditation")
    "meditation yoga",
    "meditation zen",
    "meditation quotes",
]
APPSTORE_LIST_SIZE = 10

PLAY_SEED = "photo editor"
PLAY_UNIVERSE = [
    "pinterest",
    "paypal",
    "photos",
    "photo editor",
    "photo editor free",
    "photo editor ai",
    "photo editor pro",
    "photo editor collage",
    "photo editor background remover",
    "photo editor for pictures",
    "photo editor app",
    "photo editor vintage",  # beyond the 8 Play shows for the seed: found via expansion only
    "photo editor with text",
    "photo collage",
]
PLAY_LIST_SIZE = 8


def suggestions(universe: list[str], size: int, query: str) -> list[str]:
    return [term for term in universe if term.startswith(query)][:size]


def appstore_suggest(query: str) -> list[str]:
    return suggestions(APPSTORE_UNIVERSE, APPSTORE_LIST_SIZE, query)


def play_suggest(query: str) -> list[str]:
    return suggestions(PLAY_UNIVERSE, PLAY_LIST_SIZE, query)


# ----------------------------------------------------------------------------- wire formats


def hints_payload(query: str) -> bytes:
    """MZSearchHints plist, shaped like tests/fixtures/appstore_hints.xml."""
    if query == APPSTORE_SEED:
        return HINTS_FIXTURE
    hints = [
        {
            "term": term,
            "url": f"https://search.itunes.apple.com/WebObjects/MZSearch.woa/wa/search?term={term}",
        }
        for term in appstore_suggest(query)
    ]
    return plistlib.dumps({"title": "Suggestions", "hints": hints})


def search_payload(term: str) -> bytes:
    """iTunes Search API JSON, shaped like tests/fixtures/itunes_search.json."""
    if term == APPSTORE_SEED:
        return SEARCH_FIXTURE
    rank = APPSTORE_UNIVERSE.index(term) if term in APPSTORE_UNIVERSE else len(APPSTORE_UNIVERSE)
    weight = len(APPSTORE_UNIVERSE) - rank
    apps = [
        (f"{term.title()} Pro", 40 * weight**3),
        ("Calm Mind - Daily Focus", 25 * weight**2),
        (f"{term.split()[-1].title()} Studio", 7 * weight),
    ]
    results = [
        {
            "kind": "software",
            "trackId": 1300000000 + rank * 10 + i,
            "trackName": title,
            "artistName": "Example Labs",
            "userRatingCount": ratings,
            "averageUserRating": 4.5,
            "trackViewUrl": f"https://apps.apple.com/us/app/id{1300000000 + rank * 10 + i}",
        }
        for i, (title, ratings) in enumerate(apps)
    ]
    return json.dumps({"resultCount": len(results), "results": results}).encode()


def play_payload(query: str) -> bytes:
    """batchexecute response in the chunked form of tests/fixtures/play_suggest_chunked.txt."""
    if query == PLAY_SEED:
        return PLAY_FIXTURE
    terms = play_suggest(query)
    payload = json.dumps([[[[term] for term in terms]]]) if terms else None
    chunks = [
        json.dumps([["wrb.fr", "IJ4APc", payload, None, None, None, "generic"]]),
        json.dumps([["di", 58], ["af.httprm", 57, "-4385722947291054183", 21]]),
        json.dumps([["e", 4, None, None, 320]]),
    ]
    return (")]}'\n\n" + "".join(f"{len(chunk)}\n{chunk}\n" for chunk in chunks)).encode()


def play_query(request: httpx.Request) -> str:
    form = parse_qs(request.content.decode())
    envelope = json.loads(form["f.req"][0])
    inner = json.loads(envelope[0][0][1])
    term: str = inner[0][1][0]
    return term


class FakeStores:
    """respx routes for the three store endpoints, with switchable failure modes."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.hints_mode = "ok"  # ok | 503 | html
        self.search_mode = "ok"  # ok | garbage
        self.play_mode = "ok"  # ok | garbage
        self.hints = router.get(HINTS_URL).mock(side_effect=self._hints)
        self.search = router.get(SEARCH_URL).mock(side_effect=self._search)
        self.play = router.post(SUGGEST_URL).mock(side_effect=self._play)

    @property
    def total_calls(self) -> int:
        return self.hints.call_count + self.search.call_count + self.play.call_count

    def _hints(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Apple-Store-Front"] == "143441-1,29"  # us storefront
        if self.hints_mode == "503":
            return httpx.Response(503, text="Service Unavailable")
        if self.hints_mode == "html":
            return httpx.Response(200, text="<html><body>We'll be right back</body></html>")
        query = request.url.params["term"]
        return httpx.Response(200, content=hints_payload(query), headers={"Content-Type": "text/xml"})

    def _search(self, request: httpx.Request) -> httpx.Response:
        if self.search_mode == "garbage":
            return httpx.Response(200, text="<!doctype html><title>Moved</title>")
        params = request.url.params
        assert params["country"] == "us" and params["entity"] == "software"
        return httpx.Response(200, content=search_payload(params["term"]))

    def _play(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params["rpcids"] == "IJ4APc"
        if self.play_mode == "garbage":
            return httpx.Response(200, text=")]}'\n\nnot json at all")
        return httpx.Response(200, content=play_payload(play_query(request)))


# ----------------------------------------------------------------------------- oracle


def oracle_observation(term: str, suggest: Callable[[str], list[str]]) -> tuple[int, int] | None:
    """(chars_typed, position) the engine must find: shortest surfacing prefix, best position."""
    for i in range(1, len(term) + 1):
        prefix = term[:i]
        if prefix.endswith(" "):
            continue
        listed = suggest(prefix)
        if term in listed:
            return i, listed.index(term) + 1
    return None


def expected_results(
    keywords: list[str], suggest: Callable[[str], list[str]], *, with_difficulty: bool
) -> list[tuple[str, int, int | None, int | None, int | None, int | None]]:
    rows = []
    for term in keywords:
        observed = oracle_observation(term, suggest)
        chars, pos = observed if observed else (None, None)
        popularity = popularity_score(term, chars, pos)
        difficulty = difficulty_score(term, parse_search(search_payload(term))) if with_difficulty else None
        rows.append((term, popularity, difficulty, opportunity_score(popularity, difficulty), chars, pos))
    rows.sort(key=lambda r: (-(r[3] if r[3] is not None else r[1]), -r[1], r[0]))
    return rows


def as_rows(
    results: list[KeywordResult],
) -> list[tuple[str, int, int | None, int | None, int | None, int | None]]:
    return [
        (r.keyword, r.popularity, r.difficulty, r.opportunity, r.chars_typed, r.position) for r in results
    ]


APPSTORE_KEYWORDS = [t for t in APPSTORE_UNIVERSE if t.startswith(APPSTORE_SEED)]
PLAY_KEYWORDS = [t for t in PLAY_UNIVERSE if t.startswith(PLAY_SEED)]


def test_universe_matches_recorded_fixtures() -> None:
    assert parse_hints(HINTS_FIXTURE) == appstore_suggest(APPSTORE_SEED)
    assert parse_suggest(PLAY_FIXTURE) == play_suggest(PLAY_SEED)
    assert parse_suggest(play_payload("photo editor v")) == ["photo editor vintage"]
    assert parse_hints(hints_payload("meditation a")) == ["meditation app", "meditation app free"]


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def e2e_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolated data dir and fast (but real) retry behaviour; restores global logging state."""
    data_dir = tmp_path / "data"
    monkeypatch.chdir(tmp_path)  # no stray .env
    for name in ("KEYFINDER_LOG_LEVEL", "KEYFINDER_LOG_FORMAT", "KEYFINDER_CACHE_TTL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEYFINDER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KEYFINDER_REQUEST_INTERVAL", "0")
    monkeypatch.setenv("KEYFINDER_MAX_RETRIES", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(http_module, "BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setattr(http_module, "BACKOFF_JITTER_SECONDS", 0.0)
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    get_settings.cache_clear()
    yield data_dir
    get_settings.cache_clear()
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    root.setLevel(saved_level)


@pytest.fixture
def stores() -> Iterator[FakeStores]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield FakeStores(router)


def saved_runs(data_dir: Path) -> list[ResearchRun]:
    async def load() -> list[ResearchRun]:
        repo = RunRepository(data_dir / "keyfinder.db")
        try:
            await repo.init()
            runs = []
            for summary in await repo.list(limit=100):
                run = await repo.get(summary.id)
                assert run is not None
                runs.append(run)
            return runs
        finally:
            repo.close()

    return asyncio.run(load())


# ----------------------------------------------------------------------------- CLI


def cli_research(*args: str) -> tuple[Any, ResearchRun | None]:
    result = CliRunner().invoke(cli.app, ["research", *args, "--json"])
    run = ResearchRun.model_validate_json(result.stdout) if result.exit_code == 0 else None
    return result, run


def test_cli_appstore_research_scores_and_order(stores: FakeStores, e2e_env: Path) -> None:
    result, run = cli_research(APPSTORE_SEED)

    assert result.exit_code == 0, result.output
    assert run is not None and run.status is RunStatus.SUCCEEDED
    assert as_rows(run.results) == expected_results(APPSTORE_KEYWORDS, appstore_suggest, with_difficulty=True)
    # Hand-checked against the recorded fixtures: "m" lists the seed 3rd -> 10+75+15*0.8 = 97;
    # fixture apps: median ratings 20311 and 1/3 titles match -> difficulty 58; 97*(1-.58) -> 41.
    seed = next(r for r in run.results if r.keyword == APPSTORE_SEED)
    assert (seed.popularity, seed.difficulty, seed.opportunity) == (97, 58, 41)
    assert seed.top_apps[0].title == "Stillwater: Meditation & Sleep"
    assert seed.top_apps[2].rating is None  # Apple's 0 for "unrated" is not passed through
    # Expansion discovered terms that the seed list itself did not show.
    assert {"meditation app free", "meditation yoga", "meditation zen", "meditation quotes"} <= {
        r.keyword for r in run.results
    }
    assert "maps" not in {r.keyword for r in run.results}  # probing never adds keywords
    assert run.progress.requests == stores.total_calls
    assert [r.id for r in saved_runs(e2e_env)] == [run.id]


def test_cli_play_research_scores_and_order(stores: FakeStores) -> None:
    result, run = cli_research(PLAY_SEED, "--store", "play", "--country", "gb")

    assert result.exit_code == 0, result.output
    assert run is not None
    assert as_rows(run.results) == expected_results(PLAY_KEYWORDS, play_suggest, with_difficulty=False)
    assert all(r.difficulty is None and r.opportunity is None for r in run.results)
    pops = [r.popularity for r in run.results]
    assert pops == sorted(pops, reverse=True)  # Play sorts by popularity
    assert stores.search.call_count == 0 and stores.hints.call_count == 0
    assert all(c.request.url.params["gl"] == "gb" for c in stores.play.calls)


def test_cli_second_run_is_served_from_cache(stores: FakeStores) -> None:
    first_result, first = cli_research(APPSTORE_SEED)
    assert first_result.exit_code == 0, first_result.output
    calls_after_first = stores.total_calls
    assert calls_after_first > 0

    second_result, second = cli_research(APPSTORE_SEED)

    assert second_result.exit_code == 0, second_result.output
    assert first is not None and second is not None
    assert stores.total_calls == calls_after_first  # nothing went over the wire
    assert second.progress.requests == 0
    assert second.progress.cache_hits > 0
    assert as_rows(second.results) == as_rows(first.results)

    # --no-cache bypasses it again.
    third_result, _ = cli_research(APPSTORE_SEED, "--no-cache", "--no-difficulty")
    assert third_result.exit_code == 0, third_result.output
    assert stores.total_calls > calls_after_first


def test_cli_store_503_fails_fast_and_friendly(stores: FakeStores, e2e_env: Path) -> None:
    stores.hints_mode = "503"
    started = time.monotonic()
    result, _ = cli_research(APPSTORE_SEED)

    assert time.monotonic() - started < 10
    assert result.exit_code == 1
    assert result.stdout == ""
    message = " ".join(result.stderr.split())  # Rich wraps long lines
    assert "Could not reach the App Store" in message
    assert "HTTP 503" in message
    assert "term=" not in result.stderr  # never leaks the query string
    assert "Traceback" not in result.output
    assert stores.hints.call_count == 2  # one attempt + KEYFINDER_MAX_RETRIES=1
    [run] = saved_runs(e2e_env)
    assert run.status is RunStatus.FAILED
    assert run.progress.requests == 2  # the failed attempts are counted too


def test_cli_malformed_payload_reports_parse_error(stores: FakeStores) -> None:
    stores.hints_mode = "html"
    result, _ = cli_research(APPSTORE_SEED)

    assert result.exit_code == 1
    message = " ".join(result.stderr.split())  # Rich wraps long lines
    assert "the App Store answered in an unexpected format" in message
    assert "endpoint may have changed" in message
    assert stores.hints.call_count == 1  # a parse error is not retried
    assert "Traceback" not in result.output


def test_cli_play_malformed_payload_reports_parse_error(stores: FakeStores) -> None:
    stores.play_mode = "garbage"
    result, _ = cli_research(PLAY_SEED, "--store", "play")

    assert result.exit_code == 1
    assert "Google Play answered in an unexpected format" in " ".join(result.stderr.split())


def test_cli_malformed_search_only_degrades_difficulty(stores: FakeStores) -> None:
    stores.search_mode = "garbage"
    result, run = cli_research(APPSTORE_SEED)

    assert result.exit_code == 0, result.output
    assert run is not None and run.status is RunStatus.SUCCEEDED
    assert all(r.difficulty is None and r.top_apps == [] for r in run.results)
    assert as_rows(run.results) == expected_results(
        APPSTORE_KEYWORDS, appstore_suggest, with_difficulty=False
    )


# ----------------------------------------------------------------------------- API


ApiClient = httpx.AsyncClient


@pytest.fixture
async def api(e2e_env: Path) -> AsyncIterator[ApiClient]:
    """The real app (real create_store_client, HttpClient, cache, repository) behind ASGI."""
    app = create_app(Settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def run_to_completion(api: ApiClient, body: dict[str, Any], timeout: float = 20) -> ResearchRun:
    response = await api.post("/api/runs", json=body)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert response.headers["Location"].endswith(f"/api/runs/{run_id}")
    deadline = time.monotonic() + timeout
    while True:
        polled = await api.get(f"/api/runs/{run_id}")
        assert polled.status_code == 200
        run = ResearchRun.model_validate(polled.json())
        if run.status not in (RunStatus.PENDING, RunStatus.RUNNING):
            return run
        assert time.monotonic() < deadline, f"run still {run.status} after {timeout}s"
        await asyncio.sleep(0.02)


async def test_api_appstore_lifecycle(api: ApiClient, stores: FakeStores) -> None:
    run = await run_to_completion(api, {"seed": APPSTORE_SEED, "store": "appstore", "country": "us"})

    assert run.status is RunStatus.SUCCEEDED, run.error
    assert as_rows(run.results) == expected_results(APPSTORE_KEYWORDS, appstore_suggest, with_difficulty=True)
    assert run.progress.requests == stores.total_calls
    assert run.started_at is not None and run.finished_at is not None

    history = (await api.get("/api/runs")).json()
    assert [(h["id"], h["status"], h["keyword_count"]) for h in history] == [
        (run.id, "succeeded", len(APPSTORE_KEYWORDS))
    ]

    csv_response = await api.get(f"/api/runs/{run.id}/export", params={"format": "csv"})
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert f'filename="keywords-meditation-{run.id[:8]}.csv"' in csv_response.headers["content-disposition"]
    lines = csv_response.text.splitlines()
    assert lines[0].startswith("keyword,popularity,difficulty,opportunity")
    assert lines[1].split(",")[0] == run.results[0].keyword

    assert (await api.delete(f"/api/runs/{run.id}")).status_code == 204
    assert (await api.get(f"/api/runs/{run.id}")).status_code == 404


async def test_api_play_lifecycle(api: ApiClient, stores: FakeStores) -> None:
    run = await run_to_completion(
        api, {"seed": PLAY_SEED, "store": "play", "country": "us", "language": "en", "limit": 20}
    )

    assert run.status is RunStatus.SUCCEEDED, run.error
    assert as_rows(run.results) == expected_results(PLAY_KEYWORDS, play_suggest, with_difficulty=False)
    exported = await api.get(f"/api/runs/{run.id}/export", params={"format": "json"})
    assert exported.status_code == 200
    assert ResearchRun.model_validate_json(exported.content).id == run.id


async def test_api_second_run_is_served_from_cache(api: ApiClient, stores: FakeStores) -> None:
    body = {"seed": APPSTORE_SEED, "store": "appstore", "country": "us"}
    first = await run_to_completion(api, body)
    assert first.status is RunStatus.SUCCEEDED
    calls_after_first = stores.total_calls

    second = await run_to_completion(api, body)

    assert second.status is RunStatus.SUCCEEDED
    assert stores.total_calls == calls_after_first
    assert second.progress.requests == 0
    assert second.progress.cache_hits > 0
    assert as_rows(second.results) == as_rows(first.results)


async def test_api_store_503_marks_run_failed_with_friendly_message(
    api: ApiClient, stores: FakeStores
) -> None:
    stores.hints_mode = "503"
    started = time.monotonic()
    run = await run_to_completion(api, {"seed": APPSTORE_SEED, "store": "appstore", "country": "us"})

    assert time.monotonic() - started < 10
    assert run.status is RunStatus.FAILED
    assert run.error is not None
    assert run.error.startswith("The store could not be reached.")
    assert "HTTP 503" in run.error
    assert "term=" not in run.error
    assert run.results == []
    assert run.progress.requests == stores.hints.call_count == 2  # failed attempts are counted
    export = await api.get(f"/api/runs/{run.id}/export", params={"format": "csv"})
    assert export.status_code == 409


async def test_api_malformed_payload_marks_run_failed(api: ApiClient, stores: FakeStores) -> None:
    stores.hints_mode = "html"
    run = await run_to_completion(api, {"seed": APPSTORE_SEED, "store": "appstore", "country": "us"})

    assert run.status is RunStatus.FAILED
    assert run.error is not None
    assert "unexpected format" in run.error
    assert stores.hints.call_count == 1


async def test_api_rejects_unknown_appstore_country_without_network(
    api: ApiClient, stores: FakeStores
) -> None:
    response = await api.post("/api/runs", json={"seed": APPSTORE_SEED, "store": "appstore", "country": "zz"})

    assert response.status_code == 400
    assert stores.total_calls == 0
