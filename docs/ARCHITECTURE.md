# Architecture & module contracts

Python ≥3.10, src layout, package `keyfinder`. Async throughout (httpx + asyncio).
`mypy --strict` and `ruff` (config in pyproject.toml) must pass. Every module has
`from __future__ import annotations` and full type hints. Logging via
`logging.getLogger(__name__)`. No print() outside the CLI.

```
src/keyfinder/
  __init__.py          __version__                                  [given]
  __main__.py          `python -m keyfinder` -> cli.main()
  models.py            pydantic domain models                       [given]
  config.py            Settings (KEYFINDER_* env)                   [given]
  logging_setup.py     configure_logging()
  http.py              HttpClient: rate limit, retries, stats
  cache.py             ResponseCache: SQLite TTL cache
  scoring.py           popularity / difficulty / opportunity
  research.py          ResearchEngine
  export.py            CSV / JSON export
  storage.py           RunRepository: SQLite run history
  jobs.py              JobManager: background runs for the API
  api.py               FastAPI app factory
  cli.py               Typer CLI
  stores/
    __init__.py        create_store_client() factory, re-exports
    base.py            StoreClient protocol + errors               [given]
    appstore.py        AppStoreClient + parsers + STOREFRONTS
    googleplay.py      GooglePlayClient + parsers
    cached.py          CachedStoreClient decorator
  web/static/index.html   single-page UI (no build step, no CDN)
tests/                 pytest, pytest-asyncio (asyncio_mode=auto), respx
```

Error types live in `stores/base.py`: `StoreError` > `StoreUnavailableError` >
`StoreRateLimitedError`; `StoreError` > `StoreParseError`.

---

## logging_setup.py
```python
def configure_logging(level: str = "INFO", fmt: Literal["text","json"] = "text") -> None
```
Idempotent. JSON format = one JSON object per line with `ts, level, logger, msg`
plus any `extra` fields. Quiets `httpx`/`httpcore` to WARNING.

## http.py
```python
@dataclass
class RequestStats:
    requests: int = 0     # attempts sent (incl. retries)
    retries: int = 0
    failures: int = 0     # calls that ultimately raised

class HttpClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None
    stats: RequestStats
    async def get(self, url: str, *, params: Mapping[str, str] | None = None,
                  headers: Mapping[str, str] | None = None) -> bytes
    async def post(self, url: str, *, params: Mapping[str, str] | None = None,
                   data: Mapping[str, str] | None = None,
                   headers: Mapping[str, str] | None = None) -> bytes
    async def aclose(self) -> None
    async def __aenter__(self) -> HttpClient ; async def __aexit__(...) -> None
```
- One shared `httpx.AsyncClient` (timeout=settings.request_timeout, User-Agent=settings.user_agent, follow_redirects=True). `transport` is for tests.
- Global `asyncio.Semaphore(settings.max_concurrency)`.
- Per-host politeness: at least `settings.request_interval` seconds between request *starts* to the same host (per-host asyncio.Lock + last-start timestamp, `time.monotonic`).
- Retries (up to `settings.max_retries` extra attempts) on `httpx.TransportError` (incl. timeouts), 429, 500–599. Backoff `min(30, 0.5 * 2**attempt) + random jitter(0..0.25s)`; if a `Retry-After` header (seconds) is present, honour it, capped at 60s. Backoff sleeps must NOT hold the semaphore.
- After retries exhausted: 429 → `StoreRateLimitedError`; other → `StoreUnavailableError` (message includes host and status/exception, never the full query string).
- Other 4xx → `StoreUnavailableError` immediately (no retry).
- Proxy refusals (httpx.ProxyError) are TransportErrors: retry then StoreUnavailableError.

## cache.py
```python
class ResponseCache:
    def __init__(self, path: Path, ttl_seconds: int) -> None   # ttl 0 => disabled (get always None, set no-op)
    async def get(self, namespace: str, key: str) -> Any | None   # JSON-decoded value, None if missing/expired
    async def set(self, namespace: str, key: str, value: Any) -> None   # value must be JSON-serialisable
    async def purge_expired(self) -> int
    async def clear(self) -> int
    async def stats(self) -> dict[str, int]       # {"entries": all rows incl. expired, "expired": m}
    def close(self) -> None
```
stdlib `sqlite3`, WAL mode, one connection (`check_same_thread=False`) guarded by a
`threading.Lock`, operations run via `asyncio.to_thread`. Table
`cache(namespace TEXT, key TEXT, value TEXT, expires_at REAL, PRIMARY KEY(namespace,key))`.
Creates parent dir. Uses wall clock `time.time()` for expiry.

## stores/appstore.py
```python
STOREFRONTS: dict[str, int]          # ISO country -> Apple storefront id (only ids you are confident in)
HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"
SEARCH_URL = "https://itunes.apple.com/search"
def parse_hints(raw: bytes) -> list[str]        # plist (XML or binary) or JSON {"hints":[{"term":...}]}; normalized, de-duplicated, order kept; raises StoreParseError on unrecognised payload
def parse_search(raw: bytes) -> list[AppSummary]  # iTunes Search API JSON; app_id=str(trackId), title=trackName, developer=artistName, ratings=userRatingCount, rating=averageUserRating, url=trackViewUrl
class AppStoreClient:   # implements StoreClient
    store = Store.APPSTORE ; supports_difficulty = True
    def __init__(self, http: HttpClient, country: str, language: str = "en") -> None   # ValueError for unknown country
    async def suggest(self, term) -> list[str]   # GET HINTS_URL?clientApplication=Software&term=..  header X-Apple-Store-Front: f"{id}-1,29"
    async def top_apps(self, term, limit=10) -> list[AppSummary]   # GET SEARCH_URL?term&country&entity=software&limit&lang=<language mapped to e.g. en_us>
```

## stores/googleplay.py
```python
SUGGEST_URL = "https://play.google.com/_/PlayStoreUi/data/batchexecute"
def build_suggest_form(term: str) -> dict[str, str]   # {"f.req": json [[["IJ4APc", json [[None,[term],[10],[2],4]], None, "generic"]]]}
def parse_suggest(raw: bytes) -> list[str]            # strips XSSI prefix ")]}'", finds ["wrb.fr","IJ4APc",payload,...]; payload None/empty -> []; data[0][0][i][0] are terms; normalized; StoreParseError if envelope unrecognised
class GooglePlayClient:   # implements StoreClient
    store = Store.PLAY ; supports_difficulty = False
    def __init__(self, http: HttpClient, country: str, language: str = "en") -> None
    async def suggest(self, term) -> list[str]   # POST SUGGEST_URL?rpcids=IJ4APc&hl=<language>&gl=<country> form-encoded
    async def top_apps(self, term, limit=10) -> None
```

## stores/cached.py
```python
class CachedStoreClient:   # implements StoreClient, wraps another
    def __init__(self, inner: StoreClient, cache: ResponseCache, namespace: str) -> None
    store, supports_difficulty  -> proxied from inner
    hits: int ; misses: int
    async def suggest(term) ; async def top_apps(term, limit=10)
```
Cache keys: `f"{namespace}:suggest"` / term and `f"{namespace}:top"` / f"{limit}:{term}".
Stores AppSummary lists as `model_dump()` dicts; `None` top_apps is not cached.
Errors are never cached.

## stores/__init__.py
```python
def create_store_client(params: ResearchParams, http: HttpClient,
                        cache: ResponseCache | None = None) -> StoreClient
```
Namespace = f"{store}:{country}:{language}". Re-export the error classes,
StoreClient, AppStoreClient, GooglePlayClient, CachedStoreClient, STOREFRONTS.
Type alias `StoreClientFactory = Callable[[ResearchParams, "HttpClient", "ResponseCache | None"], StoreClient]`.
**Import direction:** `http.py` imports `keyfinder.stores.base` at runtime, so nothing under
`keyfinder.stores` may import `keyfinder.http` (or `keyfinder.cache`) at runtime — only under
`TYPE_CHECKING`, with string forward references. Namespaces use `params.store.value`
(never `f"{params.store}"`, which differs between Python 3.10 and 3.11+).

## scoring.py
```python
SUGGESTION_LIST_SIZE = 10
def popularity_score(term: str, chars_typed: int | None, position: int | None,
                     list_size: int = SUGGESTION_LIST_SIZE) -> int
def difficulty_score(term: str, apps: Sequence[AppSummary]) -> int
def opportunity_score(popularity: int, difficulty: int | None) -> int | None
```
Keep the prototype's algorithms (see `git show 021cf20:keyfinder.py`), with
`position` now **1-based** (None treated as worst): popularity =
`round(10 + 75*min(prefix_part,1) + 15*position_part)`, prefix_part =
`1 - chars/len + 1/len`, position_part = `1 - (min(pos,list_size)-1)/list_size`;
`chars_typed None` → 5. Clamp 0..100. Difficulty: 65% median-ratings strength
(log10(median+1)/6, capped 1) + 35% share of titles containing every word of the
term; empty apps → 0. Opportunity = `round(pop * (1 - diff/100))`, None if diff None.

## research.py
```python
ProgressCallback = Callable[[Progress], Awaitable[None]]
class ResearchEngine:
    def __init__(self, client: StoreClient, params: ResearchParams, *,
                 on_progress: ProgressCallback | None = None,
                 concurrency: int = 4,
                 request_counter: Callable[[], int] | None = None,   # e.g. lambda: http.stats.requests
                 cache_hits: Callable[[], int] | None = None) -> None
    async def run(self) -> list[KeywordResult]
```
Algorithm (generalises the prototype, see `git show 021cf20:keyfinder.py`):
1. **Expanding**: suggest(seed) MUST succeed (any `StoreError` propagates → fail fast).
   Then `seed + " " + c` for c in a..z (bounded concurrency). If depth == 2, also
   suggest(k) for each discovered keyword k (not the seed) — new terms still count
   toward the limit. Discovered keywords capped at `params.limit` (seed always
   included, on top of limit — final list ≤ limit+1). First-seen order decides who
   gets in when capped. Individual expansion queries may fail: log warning, skip.
2. **Probing** (if params.probe): for every keyword, type prefixes term[:i] for
   i = 1..len (skip prefixes ending in space) until it appears; stop early once
   its best observed chars_typed ≤ i. Every suggestion list observed (any stage)
   updates *every known keyword* whose term startswith(query): record
   (len(query), position) if shorter, or same length and better position.
   Results only for known keywords (probing never adds keywords). Identical
   in-flight queries are de-duplicated (one request; dict of query → asyncio.Task/Future).
3. **Difficulty** (if params.difficulty and client.supports_difficulty): top_apps(k)
   per keyword; failures → warning, difficulty None for that keyword.
4. Scores via scoring.py. Keep top 5 apps per keyword in results.
   Sort: opportunity desc (None → use popularity), then popularity desc, then keyword asc.
Progress: emit on stage change and after each unit (throttle to ≤ 1 emission per
0.25s except stage changes and final DONE). Total for EXPANDING = 27 (+ depth-2
count when known), PROBING = keyword count, DIFFICULTY = keyword count.
Cancellation (CancelledError) must propagate cleanly and cancel child tasks.

## export.py
```python
CSV_COLUMNS = ["keyword","popularity","difficulty","opportunity","chars_typed","position","top_apps"]
def results_to_csv(results: Sequence[KeywordResult]) -> str   # top_apps = " | ".join(titles); guard spreadsheet formula injection (cells starting = + - @ \t \r get a leading ')
def run_to_json(run: ResearchRun) -> str   # pretty JSON of the whole run
def write_results(path: Path, results: Sequence[KeywordResult], run: ResearchRun | None = None) -> None  # by suffix .csv / .json; ValueError otherwise
```

## storage.py
```python
class RunRepository:
    def __init__(self, db_path: Path) -> None
    async def init(self) -> None                      # create schema (idempotent), WAL
    async def save(self, run: ResearchRun) -> None    # upsert
    async def get(self, run_id: str) -> ResearchRun | None
    async def list(self, limit: int = 50, offset: int = 0) -> list[RunSummary]   # newest first
    async def delete(self, run_id: str) -> bool
    async def mark_interrupted(self) -> int           # PENDING/RUNNING -> FAILED, error "interrupted by restart"
    def close(self) -> None
```
stdlib sqlite3 + asyncio.to_thread + threading.Lock, like cache.py. Table `runs`
with columns id, created_at, status, seed, store, country, keyword_count, error,
and `data` (full ResearchRun JSON). Index on created_at.

## jobs.py
```python
class JobManager:
    def __init__(self, settings: Settings, repo: RunRepository, http: HttpClient,
                 cache: ResponseCache | None, *, client_factory: StoreClientFactory = create_store_client) -> None
    async def submit(self, params: ResearchParams) -> ResearchRun   # persist PENDING, start asyncio task, return immediately
    async def get(self, run_id: str) -> ResearchRun | None          # live in-memory copy if active, else repo
    async def cancel(self, run_id: str) -> bool                     # True if it was active and is now cancelled
    async def shutdown(self) -> None                                # cancel active tasks, wait for them, persist CANCELLED
    @property
    def active_count(self) -> int
```
Concurrency: `asyncio.Semaphore(settings.max_concurrent_runs)` — submitted runs wait
as PENDING. On run: RUNNING (started_at), engine.run(), SUCCEEDED with results; on
StoreError → FAILED with user-friendly error; unexpected exception → FAILED
"internal error" (log traceback); CancelledError → CANCELLED (then re-raise if
shutting down). Persist progress at most once per second. Keep strong refs to tasks.
Engine concurrency = settings.max_concurrency; request_counter uses a per-run delta
of `http.stats.requests` (approximate when runs overlap — document it).

## api.py
```python
def create_app(settings: Settings | None = None, *, client_factory: StoreClientFactory | None = None) -> FastAPI
app = ...   # NOT module-level instantiation side effects at import; provide `def get_app() -> FastAPI` for uvicorn --factory
```
Lifespan: configure_logging, settings.ensure_dirs(), HttpClient, ResponseCache
(settings.cache_path, settings.cache_ttl_seconds), RunRepository.init +
mark_interrupted, JobManager. Shutdown: jobs.shutdown, http.aclose, cache/repo close.
Routes (JSON, pydantic models as response_model):
- `GET /healthz` → `{"status":"ok","version":__version__}` (no auth)
- `GET /api/meta` → `{"version", "stores":["appstore","play"], "countries": sorted(STOREFRONTS), "limits": {"limit":[5,300], "depth":[1,2]}, "auth_required": bool}`
- `POST /api/runs` body ResearchParams → 202 ResearchRun (422 on validation; 400 if appstore + unknown country)
- `GET /api/runs?limit=50&offset=0` → list[RunSummary]
- `GET /api/runs/{id}` → ResearchRun (404)
- `POST /api/runs/{id}/cancel` → `{"cancelled": bool}` (404 if unknown)
- `DELETE /api/runs/{id}` → 204 (409 if still active; 404 unknown)
- `GET /api/runs/{id}/export?format=csv|json` → attachment `keywords-<seed-slug>-<id8>.csv|json` (409 if not succeeded)
- `GET /` → web/static/index.html ; `/static/*` StaticFiles (use importlib.resources / Path(__file__) so it works when installed)
Auth: if settings.api_key set, every /api/* route except /api/meta requires header
`X-API-Key: <key>` or `Authorization: Bearer <key>` — constant-time compare
(`secrets.compare_digest`) — else 401 `{"detail":"invalid or missing API key"}`.
Middleware: security headers on all responses (CSP `default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'`, X-Content-Type-Options nosniff, Referrer-Policy no-referrer); request log line (method, path, status, ms). CORS only if settings.cors_origins.
Errors: JSON `{"detail": ...}` everywhere (unhandled → 500 `{"detail":"internal server error"}`).
Additional statuses: `POST /api/runs` → 429 when 100 runs are already queued/running
(`jobs.QueueFullError`), 503 while shutting down (`jobs.JobManagerClosedError`).
An empty `KEYFINDER_API_KEY` means auth off (`env_ignore_empty=True` in Settings).
`/docs` and `/redoc` are disabled (their CDN assets violate the CSP); `/openapi.json` is served.
No module-level `app`: use `uvicorn --factory keyfinder.api:get_app`.

## cli.py (Typer + Rich)
```python
app = typer.Typer(...)
def main() -> None   # entry point; maps StoreError -> exit 1 with red message, bad input -> exit 2
```
Commands:
- `keyfinder research SEED [--store appstore|play] [--country us] [--lang en] [--limit 50] [--depth 1] [--no-probe] [--no-difficulty] [-o/--output FILE(.csv|.json)] [--no-cache] [--no-save] [--json]` — runs the engine in-process (asyncio.run), Rich progress bar from Progress callbacks (to stderr), Rich table of results to stdout (`--json` prints JSON to stdout instead, no table). Saves the run to history unless --no-save (failed/cancelled runs too). Prints run id **to stderr**; `--json` prints the whole run (`run_to_json`). Exit codes: 0 ok; 1 store error / unknown or ambiguous run id / I/O; 2 invalid input or config; 130 Ctrl-C.
- `keyfinder serve [--host] [--port] [--reload]` — uvicorn with factory `keyfinder.api:get_app`.
- `keyfinder history [--limit 20]` — table of past runs.
- `keyfinder show RUN_ID [--json]` — results table of a saved run.
- `keyfinder export RUN_ID -o FILE`
- `keyfinder countries` — list supported App Store countries.
- `keyfinder cache stats|clear`
- `keyfinder --version`
Settings via get_settings() with CLI flags overriding. Tests use `typer.testing.CliRunner`
with a patched store-client factory (module-level `_client_factory` variable the tests can monkeypatch).

## web/static/index.html
Single self-contained page, vanilla JS, no external requests (CSP: self only).
Talks to the API above. Features: research form (seed, store, country select
from /api/meta, language, limit, depth, probe, difficulty); run progress view
polling `GET /api/runs/{id}` every 1s with stage + progress bar + cancel button;
results table: sortable columns, client-side filters (text contains, min
popularity, max difficulty), colour-coded score pills, expandable row showing top
apps (title, developer, ratings, link); export CSV/JSON buttons (via API export
endpoint, with API key header → blob download); history panel (GET /api/runs)
to reopen past runs, delete; API key prompt when a 401 is received (stored in
localStorage, wrapped in try/catch); light/dark via prefers-color-scheme;
responsive down to 360px; accessible (labels, focus states, aria-live status).
Escape all store-provided text (XSS: suggestions and app titles are untrusted).
