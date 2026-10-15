# ASO Keyword Finder

Find and score App Store and Google Play keywords, starting from a single seed word.

ASO Keyword Finder expands a seed keyword (for example `meditation`) into the related
searches that the stores' own autocomplete suggests. It then gives each keyword three scores
from 0 to 100:

| Score           | Meaning                                                              |
|-----------------|----------------------------------------------------------------------|
| **Popularity**  | How readily the store suggests the keyword (higher = searched more)  |
| **Difficulty**  | How entrenched the apps already ranking for it are (App Store only)  |
| **Opportunity** | Popularity discounted by difficulty, which is the column to sort by  |

It ships as a command-line tool (`keyfinder`), a small web UI, and a JSON REST API. Every run
is stored locally so you can reopen and export it later.

> **Read the [limitations](#limitations-and-honesty) first.** The scores are relative signals
> taken from public autocomplete. They are not search volumes.

---

## Contents

- [How it works](#how-it-works)
- [Limitations and honesty](#limitations-and-honesty)
- [Installation](#installation)
- [Command-line usage](#command-line-usage)
- [Web UI](#web-ui)
- [REST API](#rest-api)
- [Configuration](#configuration)
- [Data and caching](#data-and-caching)
- [Politeness and responsible use](#politeness-and-responsible-use)
- [Security](#security)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

---

## How it works

A research run has up to three stages, plus a final scoring step.

### 1. Expanding: discover keywords from autocomplete

The tool asks the store's autocomplete for suggestions for the seed, then for `seed a`,
`seed b`, ... `seed z` (27 queries). Every suggestion becomes a candidate keyword.
With `--depth 2` it also asks autocomplete about each keyword it found, which reaches further
but takes many more requests. Discovery stops at `--limit` keywords; when it has to choose,
the keywords seen first win. The seed is always kept, so a run returns at most `limit + 1`
keywords.

- **App Store:** Apple's search-hints endpoint
  (`search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints`), with a storefront
  header that selects the country.
- **Google Play:** the `IJ4APc` suggest RPC on
  `play.google.com/_/PlayStoreUi/data/batchexecute`, with language (`hl`) and country (`gl`).

The seed query must succeed or the run fails immediately. A failure on any of the other
expansion queries is logged and skipped.

### 2. Probing: estimate popularity

Autocomplete ranks the suggestions it expects people to search for most. A keyword that appears
after you type only one or two letters, near the top of the list, is searched for more than
one that only appears once you have typed most of it. The prober types each keyword one
character at a time (`m`, `me`, `med`, ...) until the keyword shows up, then records:

- `chars_typed`: the shortest prefix that surfaced it
- `position`: its 1-based position in that suggestion list

Every suggestion list the tool sees, at any stage, updates every known keyword it contains, and
identical queries are sent only once. Popularity then works out to:

```
prefix_part   = 1 - chars_typed/len(keyword) + 1/len(keyword)
position_part = 1 - (min(position, 10) - 1) / 10
popularity    = round(10 + 75 * min(prefix_part, 1) + 15 * position_part)   # clamped 0..100
```

If a keyword never appears, `chars_typed` defaults to 5 and its position counts as the worst.
`--no-probe` skips this stage. It is much faster, but the popularity scores get rougher.

### 3. Difficulty: look at who already ranks (App Store only)

For each keyword the tool fetches the top 10 apps from the public iTunes Search API, then
combines two numbers:

- **65%:** strength of the incumbents, `log10(median rating count + 1) / 6`, capped at 1
  (a median of 1,000,000 ratings scores the maximum)
- **35%:** the share of top-app titles that contain every word of the keyword

A keyword with no apps scores 0. If the lookup fails for a keyword, its difficulty is left
empty. Each result keeps the top 5 apps.

### 4. Opportunity

```
opportunity = round(popularity * (1 - difficulty / 100))
```

Results are sorted by opportunity, highest first. A keyword with no difficulty, such as any
Google Play keyword, is sorted by its popularity instead. Ties are broken by popularity, then
alphabetically.

---

## Limitations and honesty

- **The scores are relative proxies, not search counts.** Popularity shows how eagerly
  autocomplete offers a keyword. It does not say how many people search for it. Use it to
  compare keywords against each other within one store and country, not as an absolute figure.
- **Check the numbers against first-party data.** On iOS, compare against Apple's own
  *Search Popularity* score in Apple Ads (formerly Search Ads). Your Apple Ads search-term
  reports and the Google Play Console's search-term acquisition reports are the ground truth.
- **The endpoints are unofficial.** Apple's search hints and Google Play's suggest RPC are
  undocumented and can change, start returning different formats, or rate-limit or block you
  without notice. When that happens the tool reports a store error. It never makes up data.
- **Google Play has no difficulty score.** Play has no public search API, so the difficulty
  and opportunity columns are empty for Play, and results sort by popularity.
- **Coverage depends on autocomplete.** Long-tail phrases that autocomplete never suggests will
  not be discovered.
- **App Store countries are limited to the storefronts listed by `keyfinder countries`.**
  Google Play accepts any two-letter country code.

---

## Installation

Requires **Python 3.10 or newer**.

### From source with pipx (recommended for the CLI)

```bash
git clone <this repository> aso-keyword-finder
pipx install ./aso-keyword-finder
keyfinder --version
```

### From source with pip

```bash
git clone <this repository> aso-keyword-finder
cd aso-keyword-finder
python3 -m venv .venv && . .venv/bin/activate
pip install .            # or: pip install -e ".[dev]" for development
```

### Docker

```bash
docker build -t aso-keyword-finder .
docker run --rm -p 8000:8000 -v keyfinder-data:/data aso-keyword-finder
# then open http://localhost:8000
```

Or use Compose. It creates a named volume, and the API key comes from your shell or a `.env`
file:

```bash
KEYFINDER_API_KEY=change-me docker compose up -d
```

The image runs as an unprivileged user, keeps all state in `/data`, listens on `0.0.0.0:8000`
inside the container, and has a health check against `/healthz`. The default command is
`keyfinder serve`. You can run any other CLI command too:

```bash
docker run --rm -v keyfinder-data:/data aso-keyword-finder keyfinder research "habit tracker" --no-save
```

---

## Command-line usage

```text
keyfinder [--version] COMMAND [ARGS]...
```

Progress goes to stderr and results go to stdout, so piping and redirection work as you would
expect. Exit codes: `0` for success, `1` for a store error (blocked, rate-limited, unparseable
response), `2` for invalid input.

### `research`: run a keyword search

```bash
keyfinder research SEED [OPTIONS]
```

| Option                | Default    | Description                                                      |
|-----------------------|------------|------------------------------------------------------------------|
| `--store`             | `appstore` | `appstore` or `play`                                             |
| `--country`           | `us`       | Two-letter country code                                          |
| `--lang`              | `en`       | Language, e.g. `en` or `en-gb`                                   |
| `--limit`             | `50`       | Maximum keywords to discover (5 to 300; the seed is extra)       |
| `--depth`             | `1`        | `1` = seed + a..z; `2` = also expand every discovered keyword    |
| `--no-probe`          | off        | Skip prefix probing (faster, rougher popularity)                 |
| `--no-difficulty`     | off        | Skip the top-apps lookup                                         |
| `-o, --output FILE`   | none       | Also write results to `FILE.csv` or `FILE.json`                  |
| `--no-cache`          | off        | Bypass the response cache for this run                           |
| `--no-save`           | off        | Do not store the run in history                                  |
| `--json`              | off        | Print results as JSON to stdout instead of a table               |

Examples:

```bash
keyfinder research meditation
keyfinder research "budget planner" --store play --country gb --limit 100
keyfinder research meditation --no-probe --no-difficulty -o meditation.csv
keyfinder research yoga --json | jq '.[0:10]'
```

Unless you pass `--no-save`, the run is saved to history and its id is printed.

### `serve`: start the web UI and API

```bash
keyfinder serve [--host 127.0.0.1] [--port 8000] [--reload]
```

The host and port default to `KEYFINDER_HOST` and `KEYFINDER_PORT`. `--reload` is for
development only.

### `history`: list past runs

```bash
keyfinder history [--limit 20]
```

### `show`: show a saved run

```bash
keyfinder show RUN_ID [--json]
```

### `export`: export a saved run

```bash
keyfinder export RUN_ID -o keywords.csv     # or .json
```

The CSV columns are `keyword, popularity, difficulty, opportunity, chars_typed, position,
top_apps`, where `top_apps` holds the app titles joined with ` | `. Any cell that starts with
`=`, `+`, `-`, `@`, a tab or a carriage return is prefixed with `'` so spreadsheet software
cannot run it as a formula. The JSON export contains the whole run: parameters, status,
timings, progress and results.

### `countries`: list supported App Store countries

```bash
keyfinder countries
```

### `cache`: inspect or clear the response cache

```bash
keyfinder cache stats
keyfinder cache clear
```

---

## Web UI

Run `keyfinder serve` and open <http://127.0.0.1:8000>.

- A research form: seed, store, country (filled from the server), language, limit, depth, and
  probe and difficulty toggles
- A live progress view with the current stage, a progress bar and a **Cancel** button
- A results table with sortable columns, filters (text contains, minimum popularity, maximum
  difficulty) and colour-coded scores. Expand a row to see its top apps (title, developer,
  ratings, store link).
- CSV and JSON export
- A history panel to reopen or delete past runs
- A prompt for the API key if the server requires one. The key is kept in your browser's
  `localStorage`.

The page is one self-contained HTML file. It loads nothing from third-party hosts, follows your
system's light or dark preference, and works on screens as narrow as 360px.

---

## REST API

All endpoints send and receive JSON. Errors always have the form `{"detail": ...}`.
Request bodies use the `ResearchParams` model:

```json
{
  "seed": "meditation",
  "store": "appstore",
  "country": "us",
  "language": "en",
  "limit": 50,
  "depth": 1,
  "probe": true,
  "difficulty": true
}
```

| Method   | Path                              | Auth* | Description                                                                                         |
|----------|-----------------------------------|:-----:|-----------------------------------------------------------------------------------------------------|
| `GET`    | `/healthz`                        |  no   | `{"status": "ok", "version": "..."}`                                                                |
| `GET`    | `/api/meta`                       |  no   | Version, stores, App Store countries, parameter limits, and `auth_required`                         |
| `POST`   | `/api/runs`                       |  yes  | Start a run. **202** with the `ResearchRun` (status `pending`). 422 on invalid input; 400 for an unknown App Store country. |
| `GET`    | `/api/runs?limit=50&offset=0`     |  yes  | Run history, newest first (`RunSummary` list, without results)                                      |
| `GET`    | `/api/runs/{id}`                  |  yes  | The full `ResearchRun`: status, progress, error, results. 404 if not found.                        |
| `POST`   | `/api/runs/{id}/cancel`           |  yes  | `{"cancelled": true}` if the run was active and is now cancelled. 404 if not found.                |
| `DELETE` | `/api/runs/{id}`                  |  yes  | **204**. 409 if the run is still active; 404 if not found.                                          |
| `GET`    | `/api/runs/{id}/export?format=csv\|json` | yes | Download `keywords-<seed>-<id8>.csv` or `.json`. 409 if the run has not succeeded.            |
| `GET`    | `/`                               |  no   | Web UI                                                                                              |

\* Auth applies only when `KEYFINDER_API_KEY` is set. Send the key as `X-API-Key: <key>` or
`Authorization: Bearer <key>`. A missing or wrong key gets **401**
`{"detail": "invalid or missing API key"}`.

Run lifecycle: `pending` → `running` → `succeeded` | `failed` | `cancelled`. While a run is
active, `progress` reports the `stage` (`queued`, `expanding`, `probing`, `difficulty`, `done`),
`done`/`total` for that stage, and running counts of `keywords`, `requests` and `cache_hits`.
At most `KEYFINDER_MAX_CONCURRENT_RUNS` runs execute at once; the rest wait as `pending`.
When the server restarts, any run that was still pending or running is marked `failed` with the
error `interrupted by restart`.

`requests` is computed from a counter shared by the whole server, so when runs overlap each
run's count is only approximate.

Example with curl:

```bash
curl -s -X POST localhost:8000/api/runs \
     -H 'Content-Type: application/json' -H "X-API-Key: $KEYFINDER_API_KEY" \
     -d '{"seed": "meditation", "store": "appstore", "country": "us"}'
curl -s localhost:8000/api/runs/<id> -H "X-API-Key: $KEYFINDER_API_KEY"
curl -s -OJ "localhost:8000/api/runs/<id>/export?format=csv" -H "X-API-Key: $KEYFINDER_API_KEY"
```

FastAPI also generates interactive documentation at `/docs` and the schema at `/openapi.json`.

---

## Configuration

All settings come from environment variables with the `KEYFINDER_` prefix, or from a `.env`
file in the working directory (see [`.env.example`](.env.example)). CLI flags override them.

| Variable                         | Default                    | Description                                                                        |
|----------------------------------|----------------------------|------------------------------------------------------------------------------------|
| `KEYFINDER_DATA_DIR`             | `~/.keyfinder`             | Directory for the run history and the response cache                               |
| `KEYFINDER_CACHE_TTL_SECONDS`    | `86400`                    | How long store responses are cached; `0` turns the cache off                       |
| `KEYFINDER_REQUEST_INTERVAL`     | `0.3`                      | Minimum seconds between request starts to the same host                            |
| `KEYFINDER_MAX_CONCURRENCY`      | `4`                        | Maximum requests in flight across all hosts (1 to 32)                              |
| `KEYFINDER_REQUEST_TIMEOUT`      | `15.0`                     | Per-request timeout in seconds                                                     |
| `KEYFINDER_MAX_RETRIES`          | `3`                        | Extra attempts after a network error, 429 or 5xx (0 to 10)                         |
| `KEYFINDER_USER_AGENT`           | desktop Chrome UA string   | `User-Agent` header sent to the stores                                             |
| `KEYFINDER_HOST`                 | `127.0.0.1`                | Address `keyfinder serve` binds to (`0.0.0.0` in the Docker image)                 |
| `KEYFINDER_PORT`                 | `8000`                     | Port `keyfinder serve` listens on                                                  |
| `KEYFINDER_API_KEY`              | unset                      | If set, every `/api/*` route except `/api/meta` requires this key                  |
| `KEYFINDER_MAX_CONCURRENT_RUNS`  | `2`                        | Research runs the server executes at once (1 to 16); the rest queue                |
| `KEYFINDER_CORS_ORIGINS`         | `[]`                       | JSON list of origins allowed for CORS, e.g. `["https://tools.example.com"]`        |
| `KEYFINDER_LOG_LEVEL`            | `INFO`                     | `DEBUG`, `INFO`, `WARNING` or `ERROR`                                              |
| `KEYFINDER_LOG_FORMAT`           | `text`                     | `text`, or `json` for one JSON object per line (for log aggregation)              |

Invalid values, such as a negative interval or an unknown log level, are rejected at startup
with a validation error.

---

## Data and caching

Everything is stored under `KEYFINDER_DATA_DIR`. The default is `~/.keyfinder`; in Docker it
is `/data`. The directory is created on first use.

| File                    | Contents                                                                      |
|-------------------------|-------------------------------------------------------------------------------|
| `keyfinder.db`          | SQLite run history: parameters, status, progress, errors and full results    |
| `cache.db`              | SQLite cache of store responses (autocomplete lists, top apps), with a TTL   |

Both databases use SQLite WAL mode, so you may also see `-wal` and `-shm` files next to them.

- Store responses are cached for `KEYFINDER_CACHE_TTL_SECONDS` (one day by default). Each entry
  is keyed by store, country, language and query, so running the same keyword again is quick
  and polite. Errors are never cached.
- `keyfinder cache stats` shows the number of entries and how many have expired.
  `keyfinder cache clear` empties the cache. `--no-cache` skips the cache for one CLI run.
- To back up your history, copy `keyfinder.db` (ideally while the server is stopped). Deleting
  the data directory resets everything.

---

## Politeness and responsible use

This tool uses public but undocumented store endpoints. Please use them gently.

- Keep `KEYFINDER_REQUEST_INTERVAL` at **0.3 s or more** and `KEYFINDER_MAX_CONCURRENCY` low.
  The defaults leave at least 0.3 s between requests to each host.
- When a store returns `429` or `5xx`, the client backs off exponentially with jitter and
  honours `Retry-After` (capped at 60 s). Do not bypass this.
- Request counts add up quickly. A depth-1 run with probing and difficulty costs about 27
  expansion queries, plus a few prefix probes per keyword, plus one top-apps lookup per keyword.
  `--depth 2` multiplies the expansion cost. Keep the cache on and use `--no-probe` while
  exploring.
- Follow the Apple and Google terms of service that apply to you. Do not use this tool for
  bulk scraping, reselling data, or anything that would put unreasonable load on their
  services.

---

## Security

- **Bind address.** `keyfinder serve` listens on `127.0.0.1` by default, so only your own
  machine can reach it. Bind to `0.0.0.0` (as the Docker image does) only on a trusted network
  or behind a reverse proxy.
- **API key.** Set `KEYFINDER_API_KEY` to a long random value whenever anyone else can reach
  the server, for example `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
  Keys are compared in constant time. `/healthz`, `/api/meta` and the static UI do not need a
  key. The UI stores the key in the browser's `localStorage`. Never commit `.env`.
- **TLS and a reverse proxy.** The server speaks plain HTTP. When exposing it beyond localhost,
  put it behind a reverse proxy (Caddy, nginx, Traefik) that terminates TLS, so the API key is
  never sent in clear text. You can also add rate limiting or single sign-on at the proxy.
- **Response headers.** Every response carries a strict Content-Security-Policy (`self` only,
  `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
  CORS is off unless `KEYFINDER_CORS_ORIGINS` is set.
- **Untrusted data.** Suggestions and app titles come from third parties. The UI escapes them
  before display, and CSV exports defend against formula injection.
- **Container.** The Docker image runs as a non-root user and writes only to `/data`.

---

## Development

```bash
make install      # pip install -e ".[dev]"
make lint         # ruff check + ruff format --check
make format       # ruff format + ruff check --fix
make typecheck    # mypy --strict (configured in pyproject.toml)
make test         # pytest
make cov          # pytest with coverage report
make serve        # keyfinder serve --reload
make docker-build # build the image
make docker-run   # run the image on :8000 with a named data volume
make clean        # remove caches, build outputs and coverage files
```

The tests never touch the network. Store clients are tested with `respx` and recorded-format
fixtures in `tests/fixtures/`, and the engine, CLI and API use fake store clients.
Continuous integration (`.github/workflows/ci.yml`) runs lint, format check, mypy and pytest
with coverage on Python 3.10 to 3.13, and builds the Docker image.

The module layout and the contracts between modules are described in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The release history is in
[`CHANGELOG.md`](CHANGELOG.md).

---

## Troubleshooting

**`403 Forbidden` or "store unavailable" errors.** The store, or a proxy or firewall between
you and it, refused the request. Corporate proxies, VPNs, cloud and CI IP ranges are often
blocked. Try a different network, check `HTTPS_PROXY`, and run with `KEYFINDER_LOG_LEVEL=DEBUG`
to see which host failed. Other 4xx responses are not retried.

**"rate limited" errors (HTTP 429).** You hit the store's limit after all retries were used up.
Wait a few minutes, raise `KEYFINDER_REQUEST_INTERVAL` (for example to `1.0`), lower
`KEYFINDER_MAX_CONCURRENCY`, and keep the cache on so repeated runs do not hit the store again.

**Empty or very short results.** The seed may be too niche, or misspelled, or not suggested
in that country or language. Try a broader seed, a different `--country`/`--lang`, or
`--depth 2`. For the App Store, check that the country appears in `keyfinder countries`; an
unsupported country is rejected (exit code 2, or HTTP 400 from the API).

**"could not parse" errors.** The undocumented endpoint changed its response format. Update to
the latest version, or open an issue with the debug log (it does not include your query
string).

**Results look stale.** Responses are cached for a day by default. Run
`keyfinder cache clear` or pass `--no-cache`.

**A run stays `pending` in the web UI.** The server is already running
`KEYFINDER_MAX_CONCURRENT_RUNS` research runs. Wait for one to finish, or cancel it.

**`401 invalid or missing API key`.** The server has `KEYFINDER_API_KEY` set. Enter the key
when the UI asks for it, or send `X-API-Key` with API calls.
