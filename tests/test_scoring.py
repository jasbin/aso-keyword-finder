from __future__ import annotations

import pytest

from keyfinder.models import AppSummary
from keyfinder.scoring import (
    SUGGESTION_LIST_SIZE,
    UNOBSERVED_POPULARITY,
    difficulty_score,
    opportunity_score,
    popularity_score,
)


def _apps(*specs: tuple[str, int]) -> list[AppSummary]:
    return [AppSummary(app_id=str(i), title=title, ratings=r) for i, (title, r) in enumerate(specs)]


class TestPopularity:
    def test_one_char_top_position_is_max(self) -> None:
        assert popularity_score("yoga", 1, 1) == 100

    def test_unobserved_term(self) -> None:
        assert popularity_score("yoga", None, None) == UNOBSERVED_POPULARITY == 5
        assert popularity_score("yoga", None, 1) == 5

    def test_full_term_typed_worst_position(self) -> None:
        # prefix_part = 1 - 4/4 + 1/4 = 0.25 ; position_part = 1 - 9/10 = 0.1
        assert popularity_score("yoga", 4, 10) == round(10 + 75 * 0.25 + 15 * 0.1)

    def test_formula_mid_values(self) -> None:
        term = "meditation app"  # 14 chars
        prefix_part = 1 - 5 / 14 + 1 / 14
        position_part = 1 - (3 - 1) / 10
        assert popularity_score(term, 5, 3) == round(10 + 75 * prefix_part + 15 * position_part)

    def test_position_none_is_worst(self) -> None:
        assert popularity_score("yoga", 2, None) == popularity_score("yoga", 2, SUGGESTION_LIST_SIZE)

    def test_position_beyond_list_is_clamped(self) -> None:
        assert popularity_score("yoga", 2, 50) == popularity_score("yoga", 2, 10)
        assert popularity_score("yoga", 2, 0) == popularity_score("yoga", 2, 1)

    def test_fewer_chars_and_better_position_rank_higher(self) -> None:
        assert popularity_score("sleep sounds", 3, 1) > popularity_score("sleep sounds", 7, 1)
        assert popularity_score("sleep sounds", 3, 1) > popularity_score("sleep sounds", 3, 6)

    @pytest.mark.parametrize("chars", [0, 1, 3, 20, 100])
    @pytest.mark.parametrize("position", [None, 1, 5, 10, 99])
    def test_always_in_range(self, chars: int, position: int | None) -> None:
        assert 0 <= popularity_score("ab", chars, position) <= 100

    def test_custom_list_size(self) -> None:
        assert popularity_score("yoga", 1, 5, list_size=5) == round(10 + 75 + 15 * (1 - 4 / 5))


class TestDifficulty:
    def test_no_apps(self) -> None:
        assert difficulty_score("yoga", []) == 0

    def test_strong_apps_all_matching_titles(self) -> None:
        apps = _apps(("Yoga Daily", 2_000_000), ("Down Dog Yoga", 5_000_000), ("YOGA studio", 1_000_000))
        assert difficulty_score("yoga", apps) == 100

    def test_weak_apps_no_matching_titles(self) -> None:
        assert difficulty_score("yoga", _apps(("Stretch", 0), ("Pilates", 0))) == 0

    def test_formula(self) -> None:
        import math

        apps = _apps(("Sleep Sounds", 10), ("Calm", 1000), ("sounds for sleep", 100_000), ("Noise", 50))
        # sorted counts [10, 50, 1000, 100000] -> median index 2 -> 1000
        strength = math.log10(1001) / 6
        in_title = 2 / 4  # every word of the term must appear
        assert difficulty_score("sleep sounds", apps) == round(100 * (0.65 * strength + 0.35 * in_title))

    def test_case_insensitive_term(self) -> None:
        apps = _apps(("Yoga", 0))
        assert difficulty_score("YOGA", apps) == difficulty_score("yoga", apps) == 35


class TestOpportunity:
    def test_unknown_difficulty(self) -> None:
        assert opportunity_score(80, None) is None

    @pytest.mark.parametrize(
        ("pop", "diff", "expected"),
        [(80, 0, 80), (80, 100, 0), (80, 25, 60), (55, 33, round(55 * 0.67)), (0, 50, 0)],
    )
    def test_values(self, pop: int, diff: int, expected: int) -> None:
        assert opportunity_score(pop, diff) == expected
