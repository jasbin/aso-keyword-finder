"""Keyword research engine: expand a seed via store autocomplete, probe prefixes, score.

The engine only talks to a :class:`~keyfinder.stores.base.StoreClient`, so it is agnostic of
HTTP, caching and the concrete store.

Stages
------
1. **Expanding** - ``suggest(seed)`` (must succeed), then ``seed + " " + c`` for ``c`` in a..z,
   and with ``depth == 2`` also ``suggest(k)`` for each keyword discovered so far. New terms
   are admitted in deterministic first-seen order (query order, then list position) until
   ``params.limit`` keywords besides the seed are known.
2. **Probing** - for every keyword, type its prefixes until it shows up in the suggestions.
3. **Difficulty** - fetch the top-ranking apps per keyword (stores that support it only).

Every suggestion list observed in any stage records, for each term in it that starts with the
query, ``(len(query), 1-based position)`` when that is strictly better than what was recorded
before (shorter prefix, or same prefix length and higher position). Only known keywords are
reported, so probing never adds keywords. Identical queries are sent at most once per run
(concurrent duplicates share one in-flight request).
"""

from __future__ import annotations

import asyncio
import logging
import string
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

from keyfinder.models import AppSummary, KeywordResult, Progress, ResearchParams, Stage, normalize_term
from keyfinder.scoring import difficulty_score, opportunity_score, popularity_score
from keyfinder.stores.base import StoreClient, StoreError

__all__ = ["PROGRESS_MIN_INTERVAL", "TOP_APPS_KEPT", "ProgressCallback", "ResearchEngine"]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[Progress], Awaitable[None]]

PROGRESS_MIN_INTERVAL = 0.25
"""Minimum seconds between two per-unit progress emissions (stage changes are never throttled)."""

TOP_APPS_KEPT = 5
"""Number of top-ranking apps kept per keyword in the results."""

EXPANSION_LETTERS = string.ascii_lowercase

_T = TypeVar("_T")
_R = TypeVar("_R")


class ResearchEngine:
    """Runs one keyword research for ``params`` against ``client``.

    ``concurrency`` bounds the number of store calls in flight. ``request_counter`` and
    ``cache_hits`` feed the matching :class:`Progress` fields (without a request counter the
    engine reports the number of store calls it made). An engine instance is single-use.
    """

    def __init__(
        self,
        client: StoreClient,
        params: ResearchParams,
        *,
        on_progress: ProgressCallback | None = None,
        concurrency: int = 4,
        request_counter: Callable[[], int] | None = None,
        cache_hits: Callable[[], int] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._client = client
        self._params = params
        self._seed = normalize_term(params.seed)
        if not self._seed:
            raise ValueError("seed keyword is empty")
        self._on_progress = on_progress
        self._concurrency = concurrency
        self._request_counter = request_counter
        self._cache_hits = cache_hits

        # Known keywords in admission order (dict used as an ordered set); the seed is first.
        self._keywords: dict[str, None] = {}
        # Best (chars_typed, 1-based position) per term, over every list observed so far.
        self._best: dict[str, tuple[int, int]] = {}
        # One task per distinct query: de-duplicates in-flight requests and memoizes answers.
        self._queries: dict[str, asyncio.Task[list[str]]] = {}
        self._top_apps: dict[str, list[AppSummary]] = {}
        self._difficulty: dict[str, int] = {}
        self._store_calls = 0
        self._started = False

        self._progress = Progress()
        self._last_emit = float("-inf")

    # ------------------------------------------------------------------ public API

    async def run(self) -> list[KeywordResult]:
        """Run every stage and return the scored keywords, best opportunities first.

        Raises the store's error if the seed query fails. Cancellation propagates after every
        child task has been cancelled and awaited.
        """
        if self._started:
            raise RuntimeError("a ResearchEngine instance can only run once")
        self._started = True
        logger.info(
            "research started: seed=%r store=%s country=%s limit=%d depth=%d",
            self._seed,
            self._client.store.value,
            self._params.country,
            self._params.limit,
            self._params.depth,
        )
        try:
            await self._expand()
            if self._params.probe:
                await self._probe_all()
            if self._params.difficulty and self._client.supports_difficulty:
                await self._difficulty_all()
            results = self._build_results()
        finally:
            await self._cancel_queries()
        await self._set_stage(Stage.DONE, total=len(results), done=len(results))
        logger.info(
            "research finished: seed=%r keywords=%d calls=%d", self._seed, len(results), self._store_calls
        )
        return results

    # ------------------------------------------------------------------ stages

    async def _expand(self) -> None:
        letters_total = 1 + len(EXPANSION_LETTERS)
        await self._set_stage(Stage.EXPANDING, total=letters_total)

        seed_terms = await self._suggest(self._seed)  # StoreError propagates: fail fast
        self._admit(self._seed, [self._seed])
        self._admit(self._seed, seed_terms)
        await self._unit_done()

        letter_queries = [f"{self._seed} {letter}" for letter in EXPANSION_LETTERS]
        letter_answers = await self._map_bounded(letter_queries, self._expansion_query)
        for query, terms in zip(letter_queries, letter_answers, strict=True):
            self._admit(query, terms)

        if self._params.depth >= 2:
            discovered = [k for k in self._keywords if k != self._seed]
            self._progress.total = letters_total + len(discovered)
            await self._emit(force=True)
            depth_answers = await self._map_bounded(discovered, self._expansion_query)
            for query, terms in zip(discovered, depth_answers, strict=True):
                self._admit(query, terms)
        logger.info("expansion found %d keywords for %r", len(self._keywords), self._seed)

    async def _probe_all(self) -> None:
        # Shortest terms first: their prefixes are shared by longer terms, which may then stop early.
        terms = sorted(self._keywords, key=lambda t: (len(t), t))
        await self._set_stage(Stage.PROBING, total=len(terms))
        await self._map_bounded(terms, self._probe)

    async def _difficulty_all(self) -> None:
        terms = list(self._keywords)
        await self._set_stage(Stage.DIFFICULTY, total=len(terms))
        await self._map_bounded(terms, self._fetch_difficulty)

    # ------------------------------------------------------------------ units of work

    async def _expansion_query(self, query: str) -> list[str]:
        try:
            return await self._suggest(query)
        except StoreError as exc:
            logger.warning("skipping expansion query %r: %s", query, exc)
            return []
        finally:
            await self._unit_done()

    async def _probe(self, term: str) -> None:
        try:
            for i in range(1, len(term) + 1):
                prefix = term[:i]
                if prefix.endswith(" "):
                    continue
                best = self._best.get(term)
                if best is not None and best[0] <= i:
                    return
                try:
                    await self._suggest(prefix)
                except StoreError as exc:
                    logger.warning("probe query %r for %r failed: %s", prefix, term, exc)
        finally:
            await self._unit_done()

    async def _fetch_difficulty(self, term: str) -> None:
        try:
            self._store_calls += 1
            apps = await self._client.top_apps(term)
        except StoreError as exc:
            logger.warning("difficulty unavailable for %r: %s", term, exc)
        else:
            if apps is not None:
                self._top_apps[term] = list(apps)
                self._difficulty[term] = difficulty_score(term, apps)
        finally:
            await self._unit_done()

    # ------------------------------------------------------------------ suggestions

    async def _suggest(self, query: str) -> list[str]:
        """Suggestions for ``query``; identical queries share a single store call."""
        query = normalize_term(query)
        task = self._queries.get(query)
        if task is None:
            task = asyncio.ensure_future(self._fetch_suggestions(query))
            self._queries[query] = task
        # shield: one cancelled waiter must not cancel a request other waiters share;
        # run() cancels every query task itself when it stops.
        return await asyncio.shield(task)

    async def _fetch_suggestions(self, query: str) -> list[str]:
        self._store_calls += 1
        raw = await self._client.suggest(query)
        terms = _clean(raw)
        self._observe(query, terms)
        return terms

    def _observe(self, query: str, terms: list[str]) -> None:
        typed = len(query)
        for index, term in enumerate(terms):
            if not term.startswith(query):
                continue
            candidate = (typed, index + 1)
            best = self._best.get(term)
            if best is None or candidate < best:
                self._best[term] = candidate

    def _admit(self, query: str, terms: Iterable[str]) -> None:
        """Add new terms in order while fewer than ``limit`` non-seed keywords are known."""
        limit = self._params.limit
        for term in terms:
            if term in self._keywords:
                continue
            if len(self._keywords) - (self._seed in self._keywords) >= limit:
                logger.debug("keyword limit %d reached; ignoring further terms from %r", limit, query)
                return
            self._keywords[term] = None
        self._progress.keywords = len(self._keywords)

    # ------------------------------------------------------------------ results

    def _build_results(self) -> list[KeywordResult]:
        results: list[KeywordResult] = []
        for term in self._keywords:
            chars_typed, position = self._best.get(term, (None, None))
            popularity = popularity_score(term, chars_typed, position)
            difficulty = self._difficulty.get(term)
            results.append(
                KeywordResult(
                    keyword=term,
                    popularity=popularity,
                    difficulty=difficulty,
                    opportunity=opportunity_score(popularity, difficulty),
                    chars_typed=chars_typed,
                    position=position,
                    top_apps=self._top_apps.get(term, [])[:TOP_APPS_KEPT],
                )
            )
        results.sort(
            key=lambda r: (
                -(r.opportunity if r.opportunity is not None else r.popularity),
                -r.popularity,
                r.keyword,
            )
        )
        return results

    # ------------------------------------------------------------------ concurrency

    async def _map_bounded(self, items: list[_T], func: Callable[[_T], Awaitable[_R]]) -> list[_R]:
        """``func`` over ``items`` with at most ``concurrency`` running, results in input order.

        If any call raises (or the caller is cancelled) every other call is cancelled and
        awaited before the exception propagates, so no task outlives the stage.
        """
        semaphore = asyncio.Semaphore(self._concurrency)

        async def bounded(item: _T) -> _R:
            async with semaphore:
                return await func(item)

        tasks = [asyncio.ensure_future(bounded(item)) for item in items]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _cancel_queries(self) -> None:
        pending = [task for task in self._queries.values() if not task.done()]
        for task in pending:
            task.cancel()
        # Also retrieves exceptions of finished tasks nobody awaited, avoiding asyncio warnings.
        await asyncio.gather(*self._queries.values(), return_exceptions=True)

    # ------------------------------------------------------------------ progress

    async def _set_stage(self, stage: Stage, *, total: int, done: int = 0) -> None:
        self._progress.stage = stage
        self._progress.total = total
        self._progress.done = done
        self._progress.keywords = len(self._keywords)
        await self._emit(force=True)

    async def _unit_done(self) -> None:
        self._progress.done += 1
        await self._emit()

    async def _emit(self, *, force: bool = False) -> None:
        if self._on_progress is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < PROGRESS_MIN_INTERVAL:
            return
        self._last_emit = now
        self._progress.requests = self._request_counter() if self._request_counter else self._store_calls
        self._progress.cache_hits = self._cache_hits() if self._cache_hits else 0
        snapshot = self._progress.model_copy()
        try:
            await self._on_progress(snapshot)
        except Exception:
            logger.warning("progress callback failed", exc_info=True)


def _clean(terms: Iterable[str]) -> list[str]:
    """Normalize suggestions defensively: canonical form, no blanks, first occurrence kept."""
    seen: dict[str, None] = {}
    for raw in terms:
        term = normalize_term(raw)
        if term:
            seen.setdefault(term, None)
    return list(seen)
