from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import keyfinder.cli as cli
from keyfinder import __version__
from keyfinder.cache import ResponseCache
from keyfinder.config import get_settings
from keyfinder.models import AppSummary, ResearchParams, ResearchRun, RunStatus, Store
from keyfinder.storage import RunRepository
from keyfinder.stores import STOREFRONTS, StoreRateLimitedError, StoreUnavailableError

SUGGESTIONS = {
    "yoga": ["yoga", "yoga app", "yoga for beginners"],
    "yoga a": ["yoga app", "yoga at home"],
    "yoga b": ["yoga for beginners"],
}
APPS = [
    AppSummary(app_id="1", title="Yoga App Pro", developer="Example Ltd", ratings=12_000, rating=4.7),
    AppSummary(app_id="2", title="Daily Yoga Flow", developer="Example Inc", ratings=800, rating=4.2),
]


class FakeStoreClient:
    """Canned autocomplete answers; optionally raises ``error`` from every suggest() call."""

    def __init__(self, store: Store, error: BaseException | None = None) -> None:
        self.store = store
        self.supports_difficulty = store is Store.APPSTORE
        self.error = error
        self.suggest_calls = 0

    async def suggest(self, term: str) -> list[str]:
        self.suggest_calls += 1
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return list(SUGGESTIONS.get(term, []))

    async def top_apps(self, term: str, limit: int = 10) -> list[AppSummary] | None:
        if not self.supports_difficulty:
            return None
        return APPS[:limit]


class FactoryRecorder:
    """Stands in for create_store_client and remembers what it was called with."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[ResearchParams, object, object]] = []

    def __call__(self, params: ResearchParams, http: object, cache: object) -> FakeStoreClient:
        self.calls.append((params, http, cache))
        return FakeStoreClient(params.store, self.error)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at a temp data dir and restore global logging/settings state afterwards."""
    data_dir = tmp_path / "data"
    monkeypatch.chdir(tmp_path)  # no stray .env from the working directory
    for name in ("KEYFINDER_LOG_LEVEL", "KEYFINDER_LOG_FORMAT", "KEYFINDER_CACHE_TTL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KEYFINDER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KEYFINDER_REQUEST_INTERVAL", "0")
    monkeypatch.setenv("COLUMNS", "200")
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
def factory(monkeypatch: pytest.MonkeyPatch) -> FactoryRecorder:
    recorder = FactoryRecorder()
    monkeypatch.setattr(cli, "_client_factory", recorder)
    return recorder


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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


def research_json(runner: CliRunner, *args: str) -> dict[str, Any]:
    result = runner.invoke(cli.app, ["research", "yoga", "--json", *args])
    assert result.exit_code == 0, result.output
    data: dict[str, Any] = json.loads(result.stdout)
    return data


# --------------------------------------------------------------------------- research


def test_research_prints_table_and_saves_run(
    runner: CliRunner, factory: FactoryRecorder, isolated_env: Path
) -> None:
    result = runner.invoke(cli.app, ["research", "Yoga"])

    assert result.exit_code == 0, result.output
    for keyword in ("yoga app", "yoga for beginners", "yoga at home"):
        assert keyword in result.stdout
    assert "Yoga App Pro" in result.stdout  # top app column
    runs = saved_runs(isolated_env)
    assert len(runs) == 1
    run = runs[0]
    assert run.status is RunStatus.SUCCEEDED
    assert run.params.seed == "yoga"
    assert run.finished_at is not None
    assert {r.keyword for r in run.results} == {"yoga", "yoga app", "yoga for beginners", "yoga at home"}
    assert f"Saved as run {run.id}" in result.stderr
    assert run.id not in result.stdout


def test_research_passes_params_and_cache_to_factory(runner: CliRunner, factory: FactoryRecorder) -> None:
    result = runner.invoke(
        cli.app,
        [
            *("research", "yoga", "--store", "play", "--country", "GB", "--lang", "en-gb"),
            *("--limit", "5", "--depth", "2", "--no-probe", "--no-difficulty", "--no-save"),
        ],
    )

    assert result.exit_code == 0, result.output
    (params, http, cache) = factory.calls[0]
    assert params == ResearchParams(
        seed="yoga",
        store=Store.PLAY,
        country="gb",
        language="en-gb",
        limit=5,
        depth=2,
        probe=False,
        difficulty=False,
    )
    assert isinstance(cache, ResponseCache)
    assert http is not None


def test_research_no_cache_passes_none(runner: CliRunner, factory: FactoryRecorder) -> None:
    result = runner.invoke(cli.app, ["research", "yoga", "--no-cache", "--no-save"])

    assert result.exit_code == 0, result.output
    assert factory.calls[0][2] is None


def test_research_json_stdout_is_pure_json(runner: CliRunner, factory: FactoryRecorder) -> None:
    data = research_json(runner)

    assert data["status"] == "succeeded"
    assert data["params"]["seed"] == "yoga"
    keywords = [r["keyword"] for r in data["results"]]
    assert "yoga app" in keywords
    top = next(r for r in data["results"] if r["keyword"] == "yoga app")
    assert top["difficulty"] is not None
    assert top["top_apps"][0]["title"] == "Yoga App Pro"


def test_research_no_save_leaves_history_empty(
    runner: CliRunner, factory: FactoryRecorder, isolated_env: Path
) -> None:
    result = runner.invoke(cli.app, ["research", "yoga", "--no-save"])

    assert result.exit_code == 0, result.output
    assert "Saved as run" not in result.stderr
    assert saved_runs(isolated_env) == []


@pytest.mark.parametrize("suffix", [".csv", ".json"])
def test_research_writes_output_file(
    runner: CliRunner, factory: FactoryRecorder, tmp_path: Path, suffix: str
) -> None:
    target = tmp_path / "out" / f"keywords{suffix}"

    result = runner.invoke(cli.app, ["research", "yoga", "-o", str(target)])

    assert result.exit_code == 0, result.output
    text = target.read_text(encoding="utf-8")
    if suffix == ".csv":
        rows = list(csv.DictReader(io.StringIO(text)))
        assert {row["keyword"] for row in rows} >= {"yoga", "yoga app"}
        assert "Yoga App Pro" in next(row for row in rows if row["keyword"] == "yoga app")["top_apps"]
    else:
        data = json.loads(text)
        assert data["status"] == "succeeded"
        assert any(r["keyword"] == "yoga app" for r in data["results"])
    assert "Wrote" in result.stderr


def test_research_rejects_unknown_output_suffix(runner: CliRunner, factory: FactoryRecorder) -> None:
    result = runner.invoke(cli.app, ["research", "yoga", "-o", "keywords.txt"])

    assert result.exit_code == 2
    assert ".csv or .json" in result.stderr
    assert factory.calls == []  # failed before any network work


def test_research_store_error_exits_1_and_records_failed_run(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_env: Path
) -> None:
    error = StoreUnavailableError("search.itunes.apple.com is unavailable (HTTP 403; 2 attempts)")
    monkeypatch.setattr(cli, "_client_factory", FactoryRecorder(error))

    result = runner.invoke(cli.app, ["research", "yoga"])

    assert result.exit_code == 1
    assert "Could not reach the App Store" in result.stderr
    assert "HTTP 403" in result.stderr
    assert "network" in result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.output
    [run] = saved_runs(isolated_env)
    assert run.status is RunStatus.FAILED
    assert run.error is not None and "Could not reach the App Store" in run.error


def test_research_rate_limit_message(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    error = StoreRateLimitedError("play.google.com is unavailable (HTTP 429; 4 attempts)")
    monkeypatch.setattr(cli, "_client_factory", FactoryRecorder(error))

    result = runner.invoke(cli.app, ["research", "yoga", "--store", "play", "--no-save"])

    assert result.exit_code == 1
    assert "Google Play is rate-limiting requests" in result.stderr


def test_research_interrupt_exits_130_and_records_cancelled_run(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_env: Path
) -> None:
    monkeypatch.setattr(cli, "_client_factory", FactoryRecorder(KeyboardInterrupt()))

    result = runner.invoke(cli.app, ["research", "yoga"])

    assert result.exit_code == 130
    assert "Interrupted" in result.stderr
    [run] = saved_runs(isolated_env)
    assert run.status is RunStatus.CANCELLED


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--country", "usa"], "--country: country must be a two-letter ISO code"),
        (["--lang", "english"], "--lang: language must look like"),
        (["--limit", "3"], "--limit:"),
        (["--depth", "3"], "--depth:"),
        (["--country", "zz"], "country 'zz' is not supported"),
    ],
)
def test_research_invalid_input_exits_2(
    runner: CliRunner, factory: FactoryRecorder, args: list[str], expected: str
) -> None:
    result = runner.invoke(cli.app, ["research", "yoga", *args])

    assert result.exit_code == 2
    assert expected in result.stderr
    assert "Traceback" not in result.output
    assert factory.calls == []


def test_research_blank_seed_exits_2(runner: CliRunner, factory: FactoryRecorder) -> None:
    result = runner.invoke(cli.app, ["research", "   "])

    assert result.exit_code == 2
    assert "SEED" in result.stderr


def test_research_play_accepts_any_country(runner: CliRunner, factory: FactoryRecorder) -> None:
    result = runner.invoke(cli.app, ["research", "yoga", "--store", "play", "--country", "zz", "--no-save"])

    assert result.exit_code == 0, result.output


def test_invalid_environment_configuration_exits_2(
    runner: CliRunner, factory: FactoryRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYFINDER_MAX_CONCURRENCY", "0")
    get_settings.cache_clear()

    result = runner.invoke(cli.app, ["research", "yoga"])

    assert result.exit_code == 2
    assert "KEYFINDER_MAX_CONCURRENCY" in result.stderr


# --------------------------------------------------------------------------- history / show / export


def test_history_lists_saved_runs(runner: CliRunner, factory: FactoryRecorder, isolated_env: Path) -> None:
    first = research_json(runner)
    second = research_json(runner, "--store", "play")

    result = runner.invoke(cli.app, ["history"])
    assert result.exit_code == 0, result.output
    assert first["id"] in result.stdout
    assert second["id"] in result.stdout
    assert "succeeded" in result.stdout

    as_json = runner.invoke(cli.app, ["history", "--json", "--limit", "1"])
    assert as_json.exit_code == 0, as_json.output
    entries = json.loads(as_json.stdout)
    assert [entry["id"] for entry in entries] == [second["id"]]  # newest first


def test_history_empty(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["history"])

    assert result.exit_code == 0, result.output
    assert "No saved runs yet" in result.stderr


def test_history_rejects_bad_limit(runner: CliRunner) -> None:
    assert runner.invoke(cli.app, ["history", "--limit", "0"]).exit_code == 2


def test_show_saved_run(runner: CliRunner, factory: FactoryRecorder) -> None:
    run_id = research_json(runner)["id"]

    table = runner.invoke(cli.app, ["show", run_id])
    assert table.exit_code == 0, table.output
    assert "yoga for beginners" in table.stdout
    assert "succeeded" in table.stderr

    as_json = runner.invoke(cli.app, ["show", run_id, "--json"])
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.stdout)
    assert data["id"] == run_id
    assert any(r["keyword"] == "yoga for beginners" for r in data["results"])


def test_show_unknown_run_exits_1(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["show", "does-not-exist"])

    assert result.exit_code == 1
    assert "no saved run with id 'does-not-exist'" in result.stderr


@pytest.mark.parametrize("suffix", [".csv", ".json"])
def test_export_saved_run(runner: CliRunner, factory: FactoryRecorder, tmp_path: Path, suffix: str) -> None:
    run_id = research_json(runner)["id"]
    target = tmp_path / f"export{suffix}"

    result = runner.invoke(cli.app, ["export", run_id, "-o", str(target)])

    assert result.exit_code == 0, result.output
    text = target.read_text(encoding="utf-8")
    if suffix == ".csv":
        assert text.startswith("keyword,popularity,difficulty,opportunity")
    else:
        assert json.loads(text)["id"] == run_id


def test_export_requires_succeeded_run(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_env: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "_client_factory", FactoryRecorder(StoreUnavailableError("down")))
    assert runner.invoke(cli.app, ["research", "yoga"]).exit_code == 1
    [run] = saved_runs(isolated_env)

    result = runner.invoke(cli.app, ["export", run.id, "-o", str(tmp_path / "x.csv")])

    assert result.exit_code == 1
    assert "only succeeded runs can be exported" in result.stderr
    assert not (tmp_path / "x.csv").exists()


def test_export_unknown_run_and_bad_suffix(runner: CliRunner, tmp_path: Path) -> None:
    assert runner.invoke(cli.app, ["export", "nope", "-o", str(tmp_path / "x.csv")]).exit_code == 1
    assert runner.invoke(cli.app, ["export", "nope", "-o", str(tmp_path / "x.xlsx")]).exit_code == 2
    assert runner.invoke(cli.app, ["export", "nope"]).exit_code == 2  # -o is required


# --------------------------------------------------------------------------- countries / cache / misc


def test_countries(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["countries"])
    assert result.exit_code == 0, result.output
    assert "143441" in result.stdout  # us storefront
    assert f"{len(STOREFRONTS)} App Store countries" in result.stderr

    as_json = runner.invoke(cli.app, ["countries", "--json"])
    assert json.loads(as_json.stdout) == sorted(STOREFRONTS)


def _fill_cache(data_dir: Path) -> None:
    async def fill() -> None:
        cache = ResponseCache(data_dir / "cache.db", ttl_seconds=3600)
        try:
            await cache.set("appstore:us:en:suggest", "yoga", ["yoga app"])
            await cache.set("appstore:us:en:suggest", "yoga a", ["yoga at home"])
        finally:
            cache.close()

    asyncio.run(fill())


def test_cache_stats_and_clear(runner: CliRunner, isolated_env: Path) -> None:
    _fill_cache(isolated_env)

    stats = runner.invoke(cli.app, ["cache", "stats", "--json"])
    assert stats.exit_code == 0, stats.output
    data = json.loads(stats.stdout)
    assert data["entries"] == 2
    assert data["expired"] == 0
    assert data["path"] == str(isolated_env / "cache.db")

    table = runner.invoke(cli.app, ["cache", "stats"])
    assert table.exit_code == 0, table.output
    assert "Stored entries" in table.stdout

    cleared = runner.invoke(cli.app, ["cache", "clear"])
    assert cleared.exit_code == 0, cleared.output
    assert "Removed 2 cached responses" in cleared.stderr
    assert json.loads(runner.invoke(cli.app, ["cache", "stats", "--json"]).stdout)["entries"] == 0


def test_cache_commands_without_cache_file(runner: CliRunner, isolated_env: Path) -> None:
    stats = runner.invoke(cli.app, ["cache", "stats", "--json"])
    assert stats.exit_code == 0, stats.output
    assert json.loads(stats.stdout)["entries"] == 0

    cleared = runner.invoke(cli.app, ["cache", "clear"])
    assert cleared.exit_code == 0, cleared.output
    assert "already empty" in cleared.stderr
    assert not (isolated_env / "cache.db").exists()


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"keyfinder {__version__}"


def test_python_dash_m_entry_point(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "keyfinder", "--version"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"keyfinder {__version__}"


def test_serve_runs_uvicorn_with_app_factory(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("KEYFINDER_PORT", "9123")
    get_settings.cache_clear()

    default = runner.invoke(cli.app, ["serve"])
    assert default.exit_code == 0, default.output
    overridden = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0", "--port", "8080", "--reload"])
    assert overridden.exit_code == 0, overridden.output

    (args, kwargs), (_, kwargs2) = calls
    assert args == ("keyfinder.api:get_app",)
    assert kwargs["factory"] is True
    assert (kwargs["host"], kwargs["port"], kwargs["reload"]) == ("127.0.0.1", 9123, False)
    assert (kwargs2["host"], kwargs2["port"], kwargs2["reload"]) == ("0.0.0.0", 8080, True)


def test_verbose_flag_enables_info_logging(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["-v", "countries", "--json"])

    assert result.exit_code == 0, result.output
    assert logging.getLogger().level == logging.INFO


def test_no_arguments_shows_help(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, [])

    assert result.exit_code == 2
    assert "research" in result.output


def test_main_maps_unexpected_errors_to_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**_: Any) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(cli, "app", boom)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert "unexpected RuntimeError: kaput" in capsys.readouterr().err


def test_main_maps_store_errors_to_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**_: Any) -> None:
        raise StoreUnavailableError("blocked")

    monkeypatch.setattr(cli, "app", boom)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert "blocked" in capsys.readouterr().err
