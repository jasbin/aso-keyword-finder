"""Keyword scores derived from autocomplete observations and top-ranking apps.

All scores are integers on a 0-100 scale:

* **popularity** - how early a term surfaces while typing (shortest typed prefix that shows
  it, and its 1-based position in that suggestion list). A relative proxy, not a search count.
* **difficulty** - how hard it is to rank: strength of the top apps (median rating count on a
  log scale, 1M ratings = max) and how many of them carry every word of the term in their title.
* **opportunity** - popularity discounted by difficulty.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from keyfinder.models import AppSummary

__all__ = [
    "SUGGESTION_LIST_SIZE",
    "UNOBSERVED_POPULARITY",
    "difficulty_score",
    "opportunity_score",
    "popularity_score",
]

SUGGESTION_LIST_SIZE = 10
"""Number of entries a store autocomplete list shows."""

UNOBSERVED_POPULARITY = 5
"""Popularity of a term never seen in the suggestions for one of its own prefixes."""

_STRENGTH_WEIGHT = 0.65
_TITLE_WEIGHT = 0.35
_MAX_LOG_RATINGS = 6.0  # log10(1_000_000): a median of 1M ratings is maximal strength


def _clamp(value: int) -> int:
    return max(0, min(100, value))


def popularity_score(
    term: str,
    chars_typed: int | None,
    position: int | None,
    list_size: int = SUGGESTION_LIST_SIZE,
) -> int:
    """Popularity 0-100 from the shortest typed prefix that surfaced ``term``.

    ``position`` is the 1-based rank within that suggestion list; ``None`` is treated as the
    worst rank. A term that was never surfaced by one of its prefixes (``chars_typed`` is None)
    gets :data:`UNOBSERVED_POPULARITY`.
    """
    if chars_typed is None:
        return UNOBSERVED_POPULARITY
    size = max(list_size, 1)
    length = max(len(term), 1)
    prefix_part = 1.0 - min(1.0, max(chars_typed, 0) / length) + 1.0 / length
    pos = size if position is None else max(1, min(position, size))
    position_part = 1.0 - (pos - 1) / size
    return _clamp(round(10 + 75 * min(prefix_part, 1.0) + 15 * position_part))


def difficulty_score(term: str, apps: Sequence[AppSummary]) -> int:
    """Difficulty 0-100 from the apps currently ranking for ``term``; no apps means 0."""
    if not apps:
        return 0
    counts = sorted(app.ratings for app in apps)
    median = counts[len(counts) // 2]
    strength = min(1.0, math.log10(median + 1) / _MAX_LOG_RATINGS)
    words = term.lower().split()
    in_title = sum(all(word in app.title.lower() for word in words) for app in apps) / len(apps)
    return _clamp(round(100 * (_STRENGTH_WEIGHT * strength + _TITLE_WEIGHT * in_title)))


def opportunity_score(popularity: int, difficulty: int | None) -> int | None:
    """Popularity discounted by difficulty; ``None`` when difficulty is unknown."""
    if difficulty is None:
        return None
    return _clamp(round(popularity * (1 - difficulty / 100)))
