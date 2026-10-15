"""Store clients (App Store, Google Play), the caching decorator and the client factory."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from keyfinder.models import ResearchParams, Store
from keyfinder.stores.appstore import STOREFRONTS, AppStoreClient
from keyfinder.stores.base import (
    StoreClient,
    StoreError,
    StoreParseError,
    StoreRateLimitedError,
    StoreUnavailableError,
)
from keyfinder.stores.cached import CachedStoreClient
from keyfinder.stores.googleplay import GooglePlayClient

# keyfinder.http imports keyfinder.stores.base (for the error types), so importing it here at
# runtime would be circular; it is only needed for annotations.
if TYPE_CHECKING:
    from keyfinder.cache import ResponseCache
    from keyfinder.http import HttpClient

__all__ = [
    "STOREFRONTS",
    "AppStoreClient",
    "CachedStoreClient",
    "GooglePlayClient",
    "StoreClient",
    "StoreClientFactory",
    "StoreError",
    "StoreParseError",
    "StoreRateLimitedError",
    "StoreUnavailableError",
    "create_store_client",
]

StoreClientFactory = Callable[[ResearchParams, "HttpClient", "ResponseCache | None"], StoreClient]

_CLIENT_TYPES: dict[Store, Callable[[HttpClient, str, str], StoreClient]] = {
    Store.APPSTORE: AppStoreClient,
    Store.PLAY: GooglePlayClient,
}


def create_store_client(
    params: ResearchParams, http: HttpClient, cache: ResponseCache | None = None
) -> StoreClient:
    """Build the client for ``params.store``, wrapped in a CachedStoreClient when a cache is given.

    Raises ValueError for an App Store country without a known storefront.
    """
    client = _CLIENT_TYPES[params.store](http, params.country, params.language)
    if cache is None:
        return client
    namespace = f"{params.store.value}:{params.country}:{params.language}"
    return CachedStoreClient(client, cache, namespace)
