"""Store client interface and errors."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from keyfinder.models import AppSummary, Store


class StoreError(Exception):
    """Base class for store access failures."""


class StoreUnavailableError(StoreError):
    """Network failure, blocked host, or 5xx after all retries."""


class StoreRateLimitedError(StoreUnavailableError):
    """The store kept answering 429 after all retries."""


class StoreParseError(StoreError):
    """The store answered, but the payload format was not recognised (endpoint changed?)."""


@runtime_checkable
class StoreClient(Protocol):
    store: Store
    supports_difficulty: bool

    async def suggest(self, term: str) -> list[str]:
        """Autocomplete suggestions for a typed term, in display order, normalized."""
        ...

    async def top_apps(self, term: str, limit: int = 10) -> list[AppSummary] | None:
        """Top-ranking apps for a search term, or None when the store can't provide them."""
        ...
