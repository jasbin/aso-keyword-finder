from __future__ import annotations

import asyncio
import logging
import string
from collections import Counter

import pytest

import keyfinder.research as research
from keyfinder.models import AppSummary, KeywordResult, Progress, ResearchParams, Stage, Store
from keyfinder.research import ResearchEngine
from keyfinder.scoring import difficulty_score, popularity_score
from keyfinder.stores.base import StoreClient, StoreParseError, StoreUnavailableError


class FakeStoreClient:
    """In-memory StoreClient: canned answers per query, call counting, failure/latency injection."""

    def __init__(
        self,
        suggestions: dict[str, list[str]] | None = None,
        *,
        apps: dict[str, list[AppSummary] | None] | None = None,
        supports_difficulty: bool = True,
    ) -> None:
        self.store = Store.APPSTORE
        self.supports_difficulty = supports_difficulty
        self.suggestions = suggestions or {}
        self.apps = apps or {}
        self.suggest_failures: dict[str, BaseException] = {}
        self.top_failures: dict[str, BaseException] = {}
        self.delays: dict[str, float] = {}
        self.gate: asyncio.Event | None = None  # when set, suggest() waits on it
        self.suggest_calls: Counter[str] = Counter()
        self.top_calls: Counter[str] = Counter()
        self.in_flight = 0
        self.max_in_flight = 0
        self.cancelled = 0

    async def suggest(self, term: str) -> list[str]:
        self.suggest_calls[term] += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            delay = self.delays.get(term, 0)
            if delay:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)
            if term in self.suggest_failures:
                raise self.suggest_failures[term]
            return list(self.suggestions.get(term, []))
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.in_flight -= 1

    async def top_apps(self, term: str, limit: int = 10) -> list[AppSummary] | None:
        self.top_calls[term] += 1
        await asyncio.sleep(0)
        if term in self.top_failures:
            raise self.top_failures[term]
        return self.apps.get(term, [])


def params(**overrides: object) -> ResearchParams:
    base: dict[str, object] = {"seed": "yoga", "probe": False, "difficulty": False, "limit": 50}
    base.update(overrides)
    return ResearchParams.model_validate(base)


def by_keyword(results: list[KeywordResult]) -> dict[str, KeywordResult]:
    return {r.keyword: r for r in results}


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeStoreClient(), StoreClient)


# --------------------------------------------------------------------------- expansion


async def test_expansion_queries_seed_and_every_letter() -> None:
    fake = FakeStoreClient(
        {
            "yoga": ["yoga", "yoga for beginners", "yoga mat"],
            "yoga a": ["yoga app", "yoga apps free"],
            "yoga z": ["yoga zen"],
        }
    )
    results = await ResearchEngine(fake, params()).run()
    expected_queries = {"yoga"} | {f"yoga {c}" for c in string.ascii_lowercase}
    assert set(fake.suggest_calls) == expected_queries
    assert all(n == 1 for n in fake.suggest_calls.values())
    assert set(by_keyword(results)) == {
        "yoga",
        "yoga for beginners",
        "yoga mat",
        "yoga app",
        "yoga apps free",
        "yoga zen",
    }


async def test_seed_always_present_even_if_never_suggested() -> None:
    fake = FakeStoreClient({"yoga": ["pilates"]})
    results = by_keyword(await ResearchEngine(fake, params()).run())
    assert set(results) == {"yoga", "pilates"}
    assert results["yoga"].chars_typed is None
    assert results["yoga"].popularity == 5
    # "pilates" doesn't start with the query "yoga", so no observation is recorded for it.
    assert results["pilates"].chars_typed is None


async def test_seed_failure_propagates_and_stops() -> None:
    fake = FakeStoreClient()
    fake.suggest_failures["yoga"] = StoreUnavailableError("apple down")
    with pytest.raises(StoreUnavailableError, match="apple down"):
        await ResearchEngine(fake, params()).run()
    assert list(fake.suggest_calls) == ["yoga"]


async def test_expansion_failures_are_skipped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeStoreClient({"yoga": ["yoga"], "yoga a": ["yoga app"], "yoga b": ["yoga ball"]})
    fake.suggest_failures["yoga a"] = StoreUnavailableError("timeout")
    fake.suggest_failures["yoga c"] = StoreParseError("bad payload")
    with caplog.at_level(logging.WARNING, logger="keyfinder.research"):
        results = await ResearchEngine(fake, params()).run()
    assert set(by_keyword(results)) == {"yoga", "yoga ball"}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("'yoga a'" in w and "timeout" in w for w in warnings)
    assert any("'yoga c'" in w for w in warnings)


async def test_unexpected_error_propagates_and_cancels_siblings() -> None:
    fake = FakeStoreClient({"yoga": ["yoga"]})
    fake.suggest_failures["yoga b"] = RuntimeError("bug")
    for c in "cdefghijklmnopqrstuvwxyz":
        fake.delays[f"yoga {c}"] = 10
    with pytest.raises(RuntimeError, match="bug"):
        await asyncio.wait_for(ResearchEngine(fake, params()).run(), timeout=5)
    assert fake.in_flight == 0
    assert fake.cancelled > 0


async def test_defensive_normalization_and_dedup() -> None:
    fake = FakeStoreClient({"yoga": ["  YOGA  Mat ", "yoga mat", "", "   ", "Yoga Ball"]})
    results = by_keyword(await ResearchEngine(fake, params()).run())
    assert set(results) == {"yoga", "yoga mat", "yoga ball"}
    # positions are within the cleaned list: "yoga mat" 1st, "yoga ball" 2nd
    assert (results["yoga mat"].chars_typed, results["yoga mat"].position) == (4, 1)
    assert (results["yoga ball"].chars_typed, results["yoga ball"].position) == (4, 2)


async def test_seed_is_normalized() -> None:
    fake = FakeStoreClient({"yoga mat": ["yoga mat"]})
    p = params(seed="yoga mat")
    object.__setattr__(p, "seed", "  Yoga   MAT ")  # bypass the model validator
    results = await ResearchEngine(fake, p).run()
    assert [r.keyword for r in results] == ["yoga mat"]
    assert "yoga mat" in fake.suggest_calls


# --------------------------------------------------------------------------- limit


async def test_limit_caps_keywords_seed_on_top() -> None:
    fake = FakeStoreClient(
        {
            "yoga": ["yoga", "yoga 1", "yoga 2"],
            "yoga a": [f"yoga a{i}" for i in range(10)],
            "yoga b": [f"yoga b{i}" for i in range(10)],
        }
    )
    results = await ResearchEngine(fake, params(limit=5)).run()
    assert len(results) == 6
    assert set(by_keyword(results)) == {"yoga", "yoga 1", "yoga 2", "yoga a0", "yoga a1", "yoga a2"}


async def test_limit_when_seed_not_suggested() -> None:
    fake = FakeStoreClient({"yoga": [f"yoga {i}" for i in range(10)]})
    results = await ResearchEngine(fake, params(limit=5)).run()
    assert set(by_keyword(results)) == {"yoga"} | {f"yoga {i}" for i in range(5)}


async def test_first_seen_order_is_deterministic_regardless_of_completion_order() -> None:
    suggestions = {"yoga": ["yoga"]} | {f"yoga {c}": [f"yoga {c}{i}" for i in range(3)] for c in "abc"}
    fake = FakeStoreClient(suggestions)
    # Make "yoga a" finish last and "yoga c" first.
    fake.delays.update({"yoga a": 0.05, "yoga b": 0.02})
    results = await ResearchEngine(fake, params(limit=5), concurrency=8).run()
    assert set(by_keyword(results)) == {"yoga", "yoga a0", "yoga a1", "yoga a2", "yoga b0", "yoga b1"}


async def test_concurrency_is_bounded() -> None:
    fake = FakeStoreClient({"yoga": ["yoga"]})
    for c in string.ascii_lowercase:
        fake.delays[f"yoga {c}"] = 0.005
    await ResearchEngine(fake, params(), concurrency=3).run()
    assert fake.max_in_flight == 3


def test_invalid_concurrency() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        ResearchEngine(FakeStoreClient(), params(), concurrency=0)


# --------------------------------------------------------------------------- depth 2


async def test_depth_two_expands_discovered_keywords() -> None:
    fake = FakeStoreClient(
        {
            "yoga": ["yoga", "yoga mat"],
            "yoga a": ["yoga app"],
            "yoga mat": ["yoga mat", "yoga mat thick"],
            "yoga app": ["yoga app free"],
        }
    )
    progress: list[Progress] = []

    async def on_progress(p: Progress) -> None:
        progress.append(p)

    results = await ResearchEngine(fake, params(depth=2), on_progress=on_progress).run()
    assert set(by_keyword(results)) == {"yoga", "yoga mat", "yoga app", "yoga mat thick", "yoga app free"}
    # depth-2 queries for every discovered keyword except the seed; new terms are not re-expanded
    assert fake.suggest_calls["yoga mat"] == 1
    assert fake.suggest_calls["yoga app"] == 1
    assert "yoga mat thick" not in fake.suggest_calls
    assert fake.suggest_calls["yoga"] == 1
    expanding = [p for p in progress if p.stage is Stage.EXPANDING]
    assert expanding[0].total == 27
    assert expanding[-1].total == 27 + 2
    assert max(p.done for p in expanding) <= 29


async def test_depth_two_terms_count_toward_limit() -> None:
    fake = FakeStoreClient(
        {
            "yoga": ["yoga", "yoga 1", "yoga 2", "yoga 3", "yoga 4"],
            "yoga 1": ["yoga 1 x", "yoga 1 y"],
            "yoga 2": ["yoga 2 x"],
        }
    )
    results = await ResearchEngine(fake, params(depth=2, limit=5)).run()
    assert set(by_keyword(results)) == {"yoga", "yoga 1", "yoga 2", "yoga 3", "yoga 4", "yoga 1 x"}


async def test_depth_one_does_not_query_keywords() -> None:
    fake = FakeStoreClient({"yoga": ["yoga", "yoga mat"]})
    await ResearchEngine(fake, params(depth=1)).run()
    assert "yoga mat" not in fake.suggest_calls


# --------------------------------------------------------------------------- observations & probing


async def test_observation_keeps_best_prefix_and_position() -> None:
    fake = FakeStoreClient(
        {
            "yoga": ["yoga", "yoga for kids", "yoga mat"],
            "yoga f": ["yoga fit", "yoga for kids"],  # longer prefix: must not replace (4, 2)
            "yoga m": ["yoga mat"],  # same as before is not an improvement either
        }
    )
    results = by_keyword(await ResearchEngine(fake, params()).run())
    assert (results["yoga"].chars_typed, results["yoga"].position) == (4, 1)
    assert (results["yoga for kids"].chars_typed, results["yoga for kids"].position) == (4, 2)
    assert (results["yoga mat"].chars_typed, results["yoga mat"].position) == (4, 3)
    assert (results["yoga fit"].chars_typed, results["yoga fit"].position) == (6, 1)


async def test_probing_finds_shortest_prefix_and_stops_early() -> None:
    fake = FakeStoreClient(
        {
            "yoga": ["yoga", "yoga mat"],
            "y": ["youtube", "yahoo", "yoga"],
            "yo": ["yoga", "yoga mat", "yoga fit"],
        }
    )
    results = by_keyword(await ResearchEngine(fake, params(probe=True)).run())
    assert (results["yoga"].chars_typed, results["yoga"].position) == (1, 3)
    assert (results["yoga mat"].chars_typed, results["yoga mat"].position) == (2, 2)
    # Probing never adds keywords even though "y"/"yo" returned new terms.
    assert set(results) == {"yoga", "yoga mat"}
    # Each prefix queried once (shared across keywords); probing stopped at "yo".
    assert fake.suggest_calls["y"] == 1
    assert fake.suggest_calls["yo"] == 1
    assert "yog" not in fake.suggest_calls
    assert results["yoga"].popularity == popularity_score("yoga", 1, 3)


async def test_probing_skips_space_prefixes_and_respects_existing_best() -> None:
    fake = FakeStoreClient({"ab": ["ab", "ab cd"], "ab c": ["ab cd"]})
    p = params(seed="ab", probe=True)
    results = by_keyword(await ResearchEngine(fake, p).run())
    # "ab cd" was seen at chars 2 from the seed query, so probing only tries "a".
    assert (results["ab cd"].chars_typed, results["ab cd"].position) == (2, 2)
    assert "ab " not in fake.suggest_calls
    assert set(fake.suggest_calls) - {f"ab {c}" for c in string.ascii_lowercase} == {"ab", "a"}


async def test_probing_unseen_keyword_types_full_term() -> None:
    fake = FakeStoreClient({"cat": ["cat", "cat toy"]})
    fake.suggestions["cat"] = ["cat toy"]  # seed itself never suggested
    results = by_keyword(await ResearchEngine(fake, params(seed="cat", probe=True)).run())
    for prefix in ["c", "ca", "cat"]:
        assert fake.suggest_calls[prefix] == 1
    assert results["cat"].chars_typed is None
    assert "cat t" in fake.suggest_calls  # expansion query, memoized for the probe of "cat toy"
    assert fake.suggest_calls["cat t"] == 1


async def test_probe_failures_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeStoreClient({"yoga": ["yoga"], "yo": ["yoga"]})
    fake.suggest_failures["y"] = StoreUnavailableError("blocked")
    with caplog.at_level(logging.WARNING):
        results = by_keyword(await ResearchEngine(fake, params(probe=True)).run())
    assert (results["yoga"].chars_typed, results["yoga"].position) == (2, 1)
    assert any("'y'" in r.getMessage() and "blocked" in r.getMessage() for r in caplog.records)


async def test_concurrent_identical_queries_share_one_call() -> None:
    fake = FakeStoreClient({"ab": ["abc", "abd", "abe"]})
    fake.delays["a"] = 0.05  # keep "a" in flight while every probe asks for it
    results = await ResearchEngine(fake, params(seed="ab", probe=True), concurrency=8).run()
    assert set(by_keyword(results)) == {"ab", "abc", "abd", "abe"}
    assert fake.suggest_calls["a"] == 1
    assert fake.suggest_calls["ab"] == 1


async def test_in_flight_deduplication_direct() -> None:
    fake = FakeStoreClient({"x": ["xa"]})
    fake.gate = asyncio.Event()
    engine = ResearchEngine(fake, params())
    waiters = [asyncio.ensure_future(engine._suggest("x")) for _ in range(5)]
    await asyncio.sleep(0.01)
    assert fake.suggest_calls["x"] == 1
    assert fake.in_flight == 1
    fake.gate.set()
    assert await asyncio.gather(*waiters) == [["xa"]] * 5
    assert fake.suggest_calls["x"] == 1
    # completed answers are memoized too
    assert await engine._suggest(" X ") == ["xa"]
    assert fake.suggest_calls["x"] == 1


# --------------------------------------------------------------------------- difficulty


def _app(i: int, title: str, ratings: int) -> AppSummary:
    return AppSummary(app_id=str(i), title=title, ratings=ratings)


async def test_difficulty_scores_and_top_five_kept() -> None:
    mat_apps = [_app(i, f"Yoga Mat {i}", 1000 * i) for i in range(8)]
    fake = FakeStoreClient(
        {"yoga": ["yoga", "yoga mat"]},
        apps={"yoga mat": mat_apps, "yoga": []},
    )
    results = by_keyword(await ResearchEngine(fake, params(difficulty=True)).run())
    mat = results["yoga mat"]
    assert mat.difficulty == difficulty_score("yoga mat", mat_apps)
    assert mat.opportunity == round(mat.popularity * (1 - mat.difficulty / 100))
    assert [a.app_id for a in mat.top_apps] == ["0", "1", "2", "3", "4"]
    assert results["yoga"].difficulty == 0
    assert results["yoga"].opportunity == results["yoga"].popularity
    assert fake.top_calls == Counter({"yoga": 1, "yoga mat": 1})


async def test_difficulty_failure_leaves_none(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeStoreClient({"yoga": ["yoga", "yoga mat"]}, apps={"yoga": [_app(1, "Yoga", 10)]})
    fake.top_failures["yoga mat"] = StoreUnavailableError("search down")
    with caplog.at_level(logging.WARNING):
        results = by_keyword(await ResearchEngine(fake, params(difficulty=True)).run())
    assert results["yoga mat"].difficulty is None
    assert results["yoga mat"].opportunity is None
    assert results["yoga mat"].top_apps == []
    assert results["yoga"].difficulty is not None
    assert any("search down" in r.getMessage() for r in caplog.records)


async def test_top_apps_none_leaves_difficulty_none() -> None:
    fake = FakeStoreClient({"yoga": ["yoga"]}, apps={"yoga": None})
    results = await ResearchEngine(fake, params(difficulty=True)).run()
    assert results[0].difficulty is None


async def test_difficulty_skipped_when_unsupported_or_disabled() -> None:
    fake = FakeStoreClient({"yoga": ["yoga"]}, supports_difficulty=False)
    await ResearchEngine(fake, params(difficulty=True)).run()
    assert not fake.top_calls
    fake2 = FakeStoreClient({"yoga": ["yoga"]})
    await ResearchEngine(fake2, params(difficulty=False)).run()
    assert not fake2.top_calls


# --------------------------------------------------------------------------- sorting


async def test_sort_order() -> None:
    fake = FakeStoreClient(
        {"yoga": ["yoga", "yoga b", "yoga a", "yoga c"]},
        apps={
            "yoga": [_app(1, "Yoga", 1_000_000)],  # high difficulty -> low opportunity
            "yoga a": [],
            "yoga b": [],
        },
    )
    fake.top_failures["yoga c"] = StoreUnavailableError("x")  # opportunity None -> popularity used
    results = await ResearchEngine(fake, params(difficulty=True)).run()
    keys = [
        (-(r.opportunity if r.opportunity is not None else r.popularity), -r.popularity, r.keyword)
        for r in results
    ]
    assert keys == sorted(keys)
    order = [r.keyword for r in results]
    # yoga b is 2nd in the list (better popularity) -> before yoga a; yoga is last
    assert order.index("yoga b") < order.index("yoga a")
    assert order[-1] == "yoga"


async def test_ties_sorted_by_keyword() -> None:
    fake = FakeStoreClient({"yoga": ["pilates", "barre", "zumba"]})
    results = await ResearchEngine(fake, params()).run()
    assert [r.keyword for r in results] == ["barre", "pilates", "yoga", "zumba"]


# --------------------------------------------------------------------------- progress


async def test_progress_stages_and_final_done(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research, "PROGRESS_MIN_INTERVAL", 0.0)
    fake = FakeStoreClient({"yoga": ["yoga", "yoga mat"]}, apps={"yoga": []})
    progress: list[Progress] = []

    async def on_progress(p: Progress) -> None:
        progress.append(p)

    counter = iter(range(1000))
    engine = ResearchEngine(
        fake,
        params(probe=True, difficulty=True),
        on_progress=on_progress,
        request_counter=lambda: next(counter),
        cache_hits=lambda: 7,
    )
    results = await engine.run()

    stages = [p.stage for p in progress]
    assert stages[0] is Stage.EXPANDING
    assert stages[-1] is Stage.DONE
    # stages appear in order, each contiguous
    order = list(dict.fromkeys(stages))
    assert order == [Stage.EXPANDING, Stage.PROBING, Stage.DIFFICULTY, Stage.DONE]
    first_of = {s: next(p for p in progress if p.stage is s) for s in order}
    assert first_of[Stage.EXPANDING].total == 27
    assert first_of[Stage.EXPANDING].done == 0
    assert first_of[Stage.PROBING].total == 2
    assert first_of[Stage.DIFFICULTY].total == 2
    assert max(p.done for p in progress if p.stage is Stage.EXPANDING) == 27
    assert max(p.done for p in progress if p.stage is Stage.PROBING) == 2
    assert max(p.done for p in progress if p.stage is Stage.DIFFICULTY) == 2
    done = progress[-1]
    assert done.done == done.total == len(results) == done.keywords == 2
    assert done.cache_hits == 7
    requests = [p.requests for p in progress]
    assert requests == sorted(requests) and len(set(requests)) == len(requests)
    # callbacks get snapshots, not the live object
    assert len({id(p) for p in progress}) == len(progress)


async def test_progress_is_throttled_but_stage_changes_are_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research, "PROGRESS_MIN_INTERVAL", 3600.0)
    fake = FakeStoreClient({"yoga": ["yoga", "yoga mat"]})
    progress: list[Progress] = []

    async def on_progress(p: Progress) -> None:
        progress.append(p)

    await ResearchEngine(fake, params(probe=True, difficulty=True), on_progress=on_progress).run()
    assert [p.stage for p in progress] == [Stage.EXPANDING, Stage.PROBING, Stage.DIFFICULTY, Stage.DONE]


async def test_progress_requests_default_to_store_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research, "PROGRESS_MIN_INTERVAL", 0.0)
    fake = FakeStoreClient({"yoga": ["yoga"]})
    progress: list[Progress] = []

    async def on_progress(p: Progress) -> None:
        progress.append(p)

    await ResearchEngine(fake, params(), on_progress=on_progress).run()
    assert progress[-1].requests == sum(fake.suggest_calls.values()) == 27
    assert progress[-1].cache_hits == 0


async def test_failing_progress_callback_does_not_abort(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeStoreClient({"yoga": ["yoga"]})

    async def on_progress(p: Progress) -> None:
        raise ValueError("ui gone")

    with caplog.at_level(logging.WARNING):
        results = await ResearchEngine(fake, params(), on_progress=on_progress).run()
    assert [r.keyword for r in results] == ["yoga"]
    assert any("progress callback failed" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- lifecycle


async def test_engine_is_single_use() -> None:
    engine = ResearchEngine(FakeStoreClient({"yoga": ["yoga"]}), params())
    await engine.run()
    with pytest.raises(RuntimeError, match="once"):
        await engine.run()


@pytest.mark.parametrize("probe", [False, True])
async def test_cancellation_leaves_no_orphan_tasks(probe: bool) -> None:
    fake = FakeStoreClient({"yoga": ["yoga", "yoga mat"]})
    fake.gate = asyncio.Event()
    before = asyncio.all_tasks()
    if probe:
        # let expansion through, block in probing
        fake.gate.set()
        blocker = asyncio.Event()
        original = fake.suggest

        async def slow_suggest(term: str) -> list[str]:
            if len(term) < 4:
                fake.in_flight += 1
                try:
                    await blocker.wait()
                except asyncio.CancelledError:
                    fake.cancelled += 1
                    raise
                finally:
                    fake.in_flight -= 1
            return await original(term)

        fake.suggest = slow_suggest  # type: ignore[method-assign]
    task = asyncio.ensure_future(ResearchEngine(fake, params(probe=probe), concurrency=4).run())
    for _ in range(50):
        await asyncio.sleep(0)
    assert fake.in_flight > 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.in_flight == 0
    assert fake.cancelled > 0
    leftover = asyncio.all_tasks() - before - {asyncio.current_task()}
    assert not leftover, leftover


async def test_seed_failure_leaves_no_tasks() -> None:
    fake = FakeStoreClient()
    fake.suggest_failures["yoga"] = StoreParseError("changed")
    before = asyncio.all_tasks()
    with pytest.raises(StoreParseError):
        await ResearchEngine(fake, params()).run()
    assert not (asyncio.all_tasks() - before)
