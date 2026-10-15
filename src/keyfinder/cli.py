"""Command-line interface (Typer + Rich).

Data goes to stdout (a Rich table, or JSON with ``--json``); progress, status lines and errors
go to stderr, so output can be piped or redirected safely.

Exit codes: ``0`` success, ``1`` store/runtime failure (network, rate limit, unknown run, I/O),
``2`` invalid input (bad option values or invalid ``KEYFINDER_*`` configuration),
``130`` interrupted with Ctrl-C.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.progress import Progress as RichProgress
from rich.table import Table
from rich.text import Text

from keyfinder import __version__
from keyfinder.cache import ResponseCache
from keyfinder.config import Settings, get_settings
from keyfinder.export import run_to_json, write_results
from keyfinder.http import HttpClient
from keyfinder.logging_setup import configure_logging
from keyfinder.models import (
    KeywordResult,
    Progress,
    ResearchParams,
    ResearchRun,
    RunStatus,
    RunSummary,
    Stage,
    Store,
    utcnow,
)
from keyfinder.research import ResearchEngine
from keyfinder.storage import RunRepository
from keyfinder.stores import (
    STOREFRONTS,
    CachedStoreClient,
    StoreClient,
    StoreClientFactory,
    StoreError,
    StoreParseError,
    StoreRateLimitedError,
    create_store_client,
)

__all__ = ["app", "main"]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

_client_factory: StoreClientFactory = create_store_client
"""Builds the store client for a run. Module-level so tests can substitute a fake client."""

_STORE_NAMES = {Store.APPSTORE: "the App Store", Store.PLAY: "Google Play"}
_EXPORT_SUFFIXES = (".csv", ".json")
_STORAGE_ERRORS = (OSError, sqlite3.Error)
_OPTION_NAMES = {"language": "--lang", "seed": "SEED"}
_STAGE_LABELS = {
    Stage.QUEUED: "Starting",
    Stage.EXPANDING: "Expanding seed",
    Stage.PROBING: "Probing prefixes",
    Stage.DIFFICULTY: "Measuring difficulty",
    Stage.DONE: "Done",
}
_STATUS_STYLES = {
    RunStatus.PENDING: "dim",
    RunStatus.RUNNING: "cyan",
    RunStatus.SUCCEEDED: "green",
    RunStatus.FAILED: "red",
    RunStatus.CANCELLED: "yellow",
}

app = typer.Typer(
    name="keyfinder",
    help="Find and score App Store / Google Play keywords from store autocomplete.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode="rich",
)
cache_app = typer.Typer(help="Inspect or clear the response cache.", no_args_is_help=True)
app.add_typer(cache_app, name="cache")


# --------------------------------------------------------------------------- output helpers


def _stdout() -> Console:
    # Created per call so Rich always writes to the current sys.stdout (CliRunner swaps it).
    return Console(highlight=False)


def _stderr() -> Console:
    return Console(stderr=True, highlight=False)


def _fail(message: str, code: int = EXIT_FAILURE) -> NoReturn:
    """Print a red error to stderr and exit with ``code``."""
    _stderr().print(f"[bold red]Error:[/] {escape(message)}")
    raise typer.Exit(code)


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"])
        name = _OPTION_NAMES.get(field, f"--{field.replace('_', '-')}" if field else "input")
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {name}: {message}")
    return "\n".join(lines)


def _store_error_message(exc: StoreError, store: Store) -> str:
    name = _STORE_NAMES[store]
    if isinstance(exc, StoreRateLimitedError):
        return (
            f"{name} is rate-limiting requests: {exc}. Wait a while before retrying, or raise "
            "KEYFINDER_REQUEST_INTERVAL to send requests more slowly."
        )
    if isinstance(exc, StoreParseError):
        return (
            f"{name} answered in an unexpected format: {exc}. The store endpoint may have changed; "
            "try upgrading keyfinder."
        )
    return f"Could not reach {name}: {exc}. Check your network connection and proxy settings."


def _settings() -> Settings:
    """The process settings; invalid ``KEYFINDER_*`` configuration exits with code 2."""
    try:
        return get_settings()
    except ValidationError as exc:
        details = "\n".join(
            f"  KEYFINDER_{'_'.join(str(p) for p in err['loc']).upper()}: {err['msg']}"
            for err in exc.errors()
        )
        _fail(f"invalid configuration:\n{details}", EXIT_USAGE)


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run a coroutine to completion, converting Ctrl-C into a clean exit with code 130."""
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        _stderr().print("[yellow]Interrupted.[/]")
        raise typer.Exit(EXIT_INTERRUPTED) from None


@contextmanager
def _repository(settings: Settings) -> Iterator[RunRepository]:
    """The run-history repository; closed when the block exits."""
    try:
        settings.ensure_dirs()
        repo = RunRepository(settings.db_path)
    except _STORAGE_ERRORS as exc:
        _fail(f"cannot open the run history at {settings.db_path}: {exc}")
    try:
        yield repo
    finally:
        repo.close()


def _score_cell(value: int | None, *, higher_is_better: bool = True) -> Text:
    if value is None:
        return Text("-", style="dim")
    goodness = value if higher_is_better else 100 - value
    if goodness >= 67:
        style = "bold green"
    elif goodness >= 34:
        style = "yellow"
    else:
        style = "red"
    return Text(str(value), style=style)


def _optional_int(value: int | None) -> Text:
    return Text("-", style="dim") if value is None else Text(str(value))


def _results_table(results: Sequence[KeywordResult], *, title: str | None = None) -> Table:
    table = Table(title=title, header_style="bold", title_justify="left")
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Keyword", style="bold", overflow="fold")
    table.add_column("Popularity", justify="right", no_wrap=True)
    table.add_column("Difficulty", justify="right", no_wrap=True)
    table.add_column("Opportunity", justify="right", no_wrap=True)
    table.add_column("Typed", justify="right", no_wrap=True)
    table.add_column("Pos", justify="right", no_wrap=True)
    table.add_column("Top app", overflow="ellipsis", no_wrap=True, max_width=32)
    for index, result in enumerate(results, start=1):
        top_app = result.top_apps[0].title if result.top_apps else ""
        table.add_row(
            str(index),
            Text(result.keyword),
            _score_cell(result.popularity),
            _score_cell(result.difficulty, higher_is_better=False),
            _score_cell(result.opportunity),
            _optional_int(result.chars_typed),
            _optional_int(result.position),
            Text(top_app, style="dim"),
        )
    return table


def _describe_params(params: ResearchParams) -> str:
    return f"'{params.seed}' on {params.store.value} ({params.country}/{params.language})"


def _print_run(run: ResearchRun) -> None:
    """Print a run's results table (stdout) and its status line (stderr)."""
    out = _stdout()
    if run.results:
        out.print(_results_table(run.results, title=f"Keywords for {escape(_describe_params(run.params))}"))
    else:
        out.print("[dim]No keywords.[/]")
    style = _STATUS_STYLES[run.status]
    status = f"Run [bold]{escape(run.id)}[/]: [{style}]{run.status.value}[/] - {len(run.results)} keywords"
    if run.error:
        status += f" - {escape(run.error)}"
    _stderr().print(status)


def _check_output_path(output: Path | None) -> None:
    if output is not None and output.suffix.lower() not in _EXPORT_SUFFIXES:
        _fail(
            f"unsupported output file {output.name!r}: the name must end in .csv or .json",
            EXIT_USAGE,
        )


def _write_output(path: Path, run: ResearchRun) -> None:
    try:
        write_results(path, run.results, run)
    except OSError as exc:
        _fail(f"cannot write {path}: {exc}")
    _stderr().print(f"Wrote {len(run.results)} keywords to [bold]{escape(str(path))}[/]")


# --------------------------------------------------------------------------- root callback


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"keyfinder {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
    verbose: Annotated[
        int,
        typer.Option("--verbose", "-v", count=True, help="More log output on stderr (-v info, -vv debug)."),
    ] = 0,
    log_format: Annotated[
        str | None,
        typer.Option("--log-format", help="Log format: text or json. Default: KEYFINDER_LOG_FORMAT or text."),
    ] = None,
) -> None:
    """Find and score App Store / Google Play keywords from store autocomplete."""
    settings = _settings()
    # The CLI is interactive: unless asked (flag or explicit KEYFINDER_LOG_LEVEL), only
    # warnings are logged so log lines do not interleave with the progress display.
    if verbose >= 2:
        level = "DEBUG"
    elif verbose == 1:
        level = "INFO"
    elif "log_level" in settings.model_fields_set:
        level = settings.log_level
    else:
        level = "WARNING"
    fmt = log_format if log_format is not None else settings.log_format
    if fmt not in ("text", "json"):
        _fail(f"--log-format must be 'text' or 'json', not {fmt!r}", EXIT_USAGE)
    configure_logging(level, "json" if fmt == "json" else "text")


# --------------------------------------------------------------------------- research


class _ProgressDisplay:
    """Feeds engine :class:`Progress` updates into a Rich progress bar on stderr."""

    def __init__(self, bar: RichProgress) -> None:
        self._bar = bar
        self._task: TaskID = bar.add_task(
            _STAGE_LABELS[Stage.QUEUED], total=None, keywords=0, requests=0, cache_hits=0
        )

    async def __call__(self, progress: Progress) -> None:
        self._bar.update(
            self._task,
            description=_STAGE_LABELS[progress.stage],
            total=progress.total or None,
            completed=progress.done,
            keywords=progress.keywords,
            requests=progress.requests,
            cache_hits=progress.cache_hits,
        )


def _progress_bar(console: Console) -> RichProgress:
    return RichProgress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<20}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(
            "[cyan]{task.fields[keywords]}[/] keywords · {task.fields[requests]} requests"
            " · {task.fields[cache_hits]} cached"
        ),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


def _hit_counter(client: StoreClient) -> Callable[[], int] | None:
    """Cache-hit counter for progress reporting, when ``client`` is served through the cache."""
    if isinstance(client, CachedStoreClient):
        cached = client
        return lambda: cached.hits
    return None


async def _execute(
    run: ResearchRun,
    settings: Settings,
    *,
    use_cache: bool,
    on_progress: Callable[[Progress], Awaitable[None]],
) -> list[KeywordResult]:
    """Run the research engine for ``run.params``, closing the HTTP client and cache afterwards."""
    cache: ResponseCache | None = None
    if use_cache and settings.cache_ttl_seconds > 0:
        cache = ResponseCache(settings.cache_path, settings.cache_ttl_seconds)
    try:
        if cache is not None:
            await cache.purge_expired()
        async with HttpClient(settings) as http:
            client = _client_factory(run.params, http, cache)
            engine = ResearchEngine(
                client,
                run.params,
                on_progress=on_progress,
                concurrency=settings.max_concurrency,
                request_counter=lambda: http.stats.requests,
                cache_hits=_hit_counter(client),
            )
            return await engine.run()
    finally:
        if cache is not None:
            cache.close()


async def _research(
    run: ResearchRun,
    settings: Settings,
    *,
    use_cache: bool,
    on_progress: Callable[[Progress], Awaitable[None]],
) -> None:
    """Execute ``run`` in place: fill in its results and final status, error and timestamps.

    Store errors and cancellation are re-raised after ``run`` has been marked failed/cancelled.
    """
    run.status = RunStatus.RUNNING
    run.started_at = utcnow()
    try:
        run.results = await _execute(run, settings, use_cache=use_cache, on_progress=on_progress)
    except StoreError as exc:
        run.status = RunStatus.FAILED
        run.error = _store_error_message(exc, run.params.store)
        raise
    except asyncio.CancelledError:
        run.status = RunStatus.CANCELLED
        run.error = "cancelled by user"
        raise
    finally:
        run.finished_at = utcnow()
    count = len(run.results)
    run.status = RunStatus.SUCCEEDED
    run.progress = Progress(stage=Stage.DONE, done=count, total=count, keywords=count)


async def _save_run(run: ResearchRun, settings: Settings) -> None:
    with _repository(settings) as repo:
        await repo.init()
        await repo.save(run)
    logger.debug("saved run %s (%s)", run.id, run.status.value)


def _save_to_history(run: ResearchRun, settings: Settings) -> bool:
    """Save ``run``; a storage failure is reported as a warning, never fatal (results are shown anyway)."""
    try:
        _run_async(_save_run(run, settings))
    except _STORAGE_ERRORS as exc:
        logger.debug("saving run %s failed", run.id, exc_info=True)
        _stderr().print(
            f"[yellow]Warning:[/] could not save the run to {settings.db_path}: {escape(str(exc))}"
        )
        return False
    return True


@app.command()
def research(
    seed: Annotated[str, typer.Argument(help="Seed keyword, e.g. 'meditation'.", show_default=False)],
    store: Annotated[Store, typer.Option(help="Store to research.")] = Store.APPSTORE,
    country: Annotated[str, typer.Option(help="Two-letter country code.")] = "us",
    lang: Annotated[str, typer.Option("--lang", help="Language, e.g. 'en' or 'en-gb'.")] = "en",
    limit: Annotated[int, typer.Option(help="Maximum keywords to discover (5-300; the seed is extra).")] = 50,
    depth: Annotated[int, typer.Option(help="1 = seed + a..z; 2 = also expand every keyword found.")] = 1,
    probe: Annotated[
        bool, typer.Option("--probe/--no-probe", help="Probe prefixes to estimate popularity.")
    ] = True,
    difficulty: Annotated[
        bool, typer.Option("--difficulty/--no-difficulty", help="Look up top apps to score difficulty.")
    ] = True,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Also write results to a .csv or .json file.", dir_okay=False),
    ] = None,
    use_cache: Annotated[
        bool, typer.Option("--cache/--no-cache", help="Use the response cache for this run.")
    ] = True,
    save: Annotated[bool, typer.Option("--save/--no-save", help="Save the run to history.")] = True,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the run as JSON on stdout instead of a table.")
    ] = False,
) -> None:
    """Expand SEED via store autocomplete and score every keyword found."""
    settings = _settings()
    try:
        params = ResearchParams(
            seed=seed,
            store=store,
            country=country,
            language=lang,
            limit=limit,
            depth=depth,
            probe=probe,
            difficulty=difficulty,
        )
    except ValidationError as exc:
        _fail(f"invalid input:\n{_format_validation_error(exc)}", EXIT_USAGE)
    if params.store is Store.APPSTORE and params.country not in STOREFRONTS:
        _fail(
            f"the App Store country {params.country!r} is not supported. "
            "Run 'keyfinder countries' to list the supported ones.",
            EXIT_USAGE,
        )
    _check_output_path(output)

    run = ResearchRun(id=uuid.uuid4().hex, params=params)
    err = _stderr()
    err.print(f"Researching [bold]{escape(_describe_params(params))}[/]")
    try:
        with _progress_bar(err) as bar:
            _run_async(_research(run, settings, use_cache=use_cache, on_progress=_ProgressDisplay(bar)))
    except StoreError as exc:
        if save:
            _save_to_history(run, settings)
        _fail(_store_error_message(exc, params.store))
    except typer.Exit:  # interrupted with Ctrl-C
        if save and run.started_at is not None:
            if not run.status.finished:
                run.status, run.error, run.finished_at = RunStatus.CANCELLED, "cancelled by user", utcnow()
            _save_to_history(run, settings)
        raise
    except ValueError as exc:  # e.g. the store client rejecting the country/language
        _fail(str(exc), EXIT_USAGE)
    except _STORAGE_ERRORS as exc:
        _fail(f"cannot access the data directory {settings.data_dir}: {exc}")

    if as_json:
        typer.echo(run_to_json(run))
    else:
        _print_run(run)
    if output is not None:
        _write_output(output, run)
    if save and _save_to_history(run, settings):
        err.print(f"Saved as run [bold]{run.id}[/] (see 'keyfinder show {run.id}')")


# --------------------------------------------------------------------------- history / show / export


async def _list_runs(settings: Settings, limit: int) -> list[RunSummary]:
    with _repository(settings) as repo:
        await repo.init()
        return await repo.list(limit=limit)


async def _get_run(settings: Settings, run_id: str) -> ResearchRun | None:
    with _repository(settings) as repo:
        await repo.init()
        return await repo.get(run_id)


def _load_run(settings: Settings, run_id: str) -> ResearchRun:
    try:
        run = _run_async(_get_run(settings, run_id))
    except _STORAGE_ERRORS as exc:
        _fail(f"cannot read the run history at {settings.db_path}: {exc}")
    if run is None:
        _fail(f"no saved run with id {run_id!r}. Run 'keyfinder history' to list saved runs.")
    return run


@app.command()
def history(
    limit: Annotated[int, typer.Option(min=1, max=1000, help="Number of runs to show.")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Print the runs as JSON.")] = False,
) -> None:
    """List past research runs, newest first."""
    settings = _settings()
    try:
        runs = _run_async(_list_runs(settings, limit))
    except _STORAGE_ERRORS as exc:
        _fail(f"cannot read the run history at {settings.db_path}: {exc}")

    if as_json:
        typer.echo(json.dumps([run.model_dump(mode="json") for run in runs], indent=2, ensure_ascii=False))
        return
    if not runs:
        _stderr().print("No saved runs yet. Start one with 'keyfinder research SEED'.")
        return
    table = Table(header_style="bold")
    table.add_column("Run ID", no_wrap=True)
    table.add_column("Created (UTC)", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Seed", overflow="fold")
    table.add_column("Store", no_wrap=True)
    table.add_column("Country", no_wrap=True)
    table.add_column("Keywords", justify="right", no_wrap=True)
    for summary in runs:
        table.add_row(
            summary.id,
            summary.created_at.strftime("%Y-%m-%d %H:%M"),
            Text(summary.status.value, style=_STATUS_STYLES[summary.status]),
            Text(summary.params.seed),
            summary.params.store.value,
            summary.params.country,
            str(summary.keyword_count),
        )
    _stdout().print(table)


@app.command()
def show(
    run_id: Annotated[str, typer.Argument(help="Run id, as listed by 'keyfinder history'.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print the whole run as JSON.")] = False,
) -> None:
    """Show the results of a saved run."""
    run = _load_run(_settings(), run_id)
    if as_json:
        typer.echo(run_to_json(run))
    else:
        _print_run(run)


@app.command()
def export(
    run_id: Annotated[str, typer.Argument(help="Run id, as listed by 'keyfinder history'.")],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Destination file (.csv or .json).", dir_okay=False),
    ],
) -> None:
    """Export a saved run to a CSV or JSON file."""
    _check_output_path(output)
    run = _load_run(_settings(), run_id)
    if run.status is not RunStatus.SUCCEEDED:
        _fail(f"run {run_id} has status {run.status.value}; only succeeded runs can be exported")
    _write_output(output, run)


# --------------------------------------------------------------------------- countries / serve


@app.command()
def countries(
    as_json: Annotated[bool, typer.Option("--json", help="Print the country codes as JSON.")] = False,
) -> None:
    """List the supported App Store countries (Google Play accepts any two-letter code)."""
    codes = sorted(STOREFRONTS)
    if as_json:
        typer.echo(json.dumps(codes))
        return
    table = Table(header_style="bold")
    table.add_column("Country", no_wrap=True)
    table.add_column("Storefront ID", justify="right", no_wrap=True)
    for code in codes:
        table.add_row(code, str(STOREFRONTS[code]))
    _stdout().print(table)
    _stderr().print(f"{len(codes)} App Store countries. Google Play accepts any two-letter country code.")


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind address. Default: KEYFINDER_HOST.")] = None,
    port: Annotated[int | None, typer.Option(min=1, max=65535, help="Port. Default: KEYFINDER_PORT.")] = None,
    reload: Annotated[bool, typer.Option(help="Restart on code changes (development only).")] = False,
) -> None:
    """Start the web UI and REST API."""
    import uvicorn  # imported lazily: only this command needs the server stack

    settings = _settings()
    bind_host = host if host is not None else settings.host
    bind_port = port if port is not None else settings.port
    _stderr().print(f"Serving keyfinder on [bold]http://{bind_host}:{bind_port}[/] (Ctrl-C to stop)")
    uvicorn.run(
        "keyfinder.api:get_app",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
        # The app configures logging itself and logs every request, so keep uvicorn's
        # own logging config and access log out of the way.
        log_config=None,
        access_log=False,
    )


# --------------------------------------------------------------------------- cache


async def _cache_stats(settings: Settings) -> dict[str, int]:
    cache = ResponseCache(settings.cache_path, settings.cache_ttl_seconds)
    try:
        return await cache.stats()
    finally:
        cache.close()


async def _cache_clear(settings: Settings) -> int:
    cache = ResponseCache(settings.cache_path, settings.cache_ttl_seconds)
    try:
        return await cache.clear()
    finally:
        cache.close()


@cache_app.command("stats")
def cache_stats(
    as_json: Annotated[bool, typer.Option("--json", help="Print the statistics as JSON.")] = False,
) -> None:
    """Show how many responses are cached."""
    settings = _settings()
    if settings.cache_path.exists():
        try:
            stats = _run_async(_cache_stats(settings))
        except _STORAGE_ERRORS as exc:
            _fail(f"cannot read the cache at {settings.cache_path}: {exc}")
    else:
        stats = {"entries": 0, "expired": 0}
    if as_json:
        typer.echo(
            json.dumps({**stats, "path": str(settings.cache_path), "ttl_seconds": settings.cache_ttl_seconds})
        )
        return
    ttl = settings.cache_ttl_seconds
    table = Table(show_header=False, box=None)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Cache file", escape(str(settings.cache_path)))
    table.add_row("TTL", f"{ttl} s" if ttl else "disabled (KEYFINDER_CACHE_TTL_SECONDS=0)")
    table.add_row("Stored entries", str(stats["entries"]))
    table.add_row("  of which expired", str(stats["expired"]))
    _stdout().print(table)


@cache_app.command("clear")
def cache_clear() -> None:
    """Delete every cached response."""
    settings = _settings()
    if not settings.cache_path.exists():
        _stderr().print("The cache is already empty.")
        return
    try:
        removed = _run_async(_cache_clear(settings))
    except _STORAGE_ERRORS as exc:
        _fail(f"cannot clear the cache at {settings.cache_path}: {exc}")
    _stderr().print(f"Removed {removed} cached responses.")


# --------------------------------------------------------------------------- entry point


def main() -> None:
    """Console-script entry point.

    Commands map expected failures to exit codes themselves (store errors 1, bad input 2);
    this is the last line of defence for anything unexpected, so users get a one-line
    message instead of a traceback (the traceback is logged at debug level).
    """
    try:
        app(prog_name="keyfinder")
    except StoreError as exc:
        logger.debug("store error", exc_info=True)
        _stderr().print(f"[bold red]Error:[/] {escape(str(exc))}")
        raise SystemExit(EXIT_FAILURE) from None
    except ValidationError as exc:
        _stderr().print(f"[bold red]Error:[/] invalid input:\n{escape(_format_validation_error(exc))}")
        raise SystemExit(EXIT_USAGE) from None
    except KeyboardInterrupt:
        _stderr().print("[yellow]Interrupted.[/]")
        raise SystemExit(EXIT_INTERRUPTED) from None
    except Exception as exc:
        logger.debug("unexpected error", exc_info=True)
        _stderr().print(
            f"[bold red]Error:[/] unexpected {type(exc).__name__}: {escape(str(exc))} "
            "(re-run with -vv for details)"
        )
        raise SystemExit(EXIT_FAILURE) from None
