# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-15

First production release, rewritten from the single-file prototype.

### Added

- `keyfinder` package (`src/` layout), installable with pip/pipx, Python 3.10–3.13.
- Async store clients for App Store (search hints + iTunes Search API) and Google Play
  (`IJ4APc` suggest RPC), with parsers covered by recorded-format fixtures.
- Shared HTTP client with global concurrency limit, per-host request spacing, retries with
  exponential backoff and jitter, `Retry-After` support, and request statistics.
- SQLite response cache with TTL (`KEYFINDER_CACHE_TTL_SECONDS`) and `keyfinder cache stats|clear`.
- Research engine with seed + a–z expansion, optional depth-2 expansion, de-duplicated
  prefix probing, difficulty from top apps, progress reporting and clean cancellation.
- Persistent run history (SQLite) with `history`, `show` and `export` commands.
- CSV / JSON export with spreadsheet formula-injection protection.
- Typer/Rich CLI: `research`, `serve`, `history`, `show`, `export`, `countries`, `cache`, `--version`.
- FastAPI REST API with background job manager, run cancellation, export endpoint, optional
  API-key authentication, security headers and optional CORS.
- Self-contained web UI: research form, live progress, sortable/filterable results, top-apps
  detail, export, history, light/dark themes.
- Configuration via `KEYFINDER_*` environment variables or `.env`; text or JSON logging.
- Dockerfile (multi-stage, non-root, health check), docker-compose, Makefile and GitHub
  Actions CI (lint, format, mypy, tests with coverage, Docker build).

### Changed

- Popularity `position` is now 1-based; scoring algorithms are otherwise unchanged from the
  prototype.

### Removed

- The stdlib-only `keyfinder.py` prototype script.
