from __future__ import annotations

import json
import plistlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from keyfinder.models import AppSummary, Store
from keyfinder.stores.appstore import (
    HINTS_URL,
    SEARCH_URL,
    STOREFRONTS,
    AppStoreClient,
    parse_hints,
    parse_search,
)
from keyfinder.stores.base import StoreClient, StoreParseError, StoreUnavailableError

FIXTURES = Path(__file__).parent / "fixtures"
HINTS_XML = (FIXTURES / "appstore_hints.xml").read_bytes()
SEARCH_JSON = (FIXTURES / "itunes_search.json").read_bytes()

EXPECTED_HINTS = [
    "meditation",
    "meditation app",
    "meditation for sleep",
    "meditation music",
    "meditation timer",
    "meditation for beginners",
    "meditation for anxiety",
    "meditation sounds",
    "meditation for kids",
    "meditation bell",
]

EXPECTED_APPS = [
    AppSummary(
        app_id="1200000001",
        title="Stillwater: Meditation & Sleep",
        developer="Stillwater Labs, Inc.",
        ratings=1544210,
        rating=4.79823,
        url="https://apps.apple.com/us/app/stillwater-meditation-sleep/id1200000001?uo=4",
    ),
    AppSummary(
        app_id="1200000002",
        title="Quiet Hour - Mindfulness",
        developer="Quiet Hour Studio LLC",
        ratings=20311,
        rating=4.6,
        url="https://apps.apple.com/us/app/quiet-hour-mindfulness/id1200000002?uo=4",
    ),
    AppSummary(
        app_id="1200000003",
        title="Zen Timer",
        developer="Jane Doe",
        ratings=0,
        rating=None,  # Apple reports averageUserRating 0 for unrated apps
        url="https://apps.apple.com/us/app/zen-timer/id1200000003?uo=4",
    ),
]


class FakeHttp:
    """Stand-in for keyfinder.http.HttpClient: records each request, answers with canned bodies."""

    def __init__(self, *responses: bytes | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def get(
        self, url: str, *, params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None
    ) -> bytes:
        self.calls.append({"method": "GET", "url": url, "params": params, "headers": headers})
        return self._next()

    async def post(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        self.calls.append({"method": "POST", "url": url, "params": params, "data": data, "headers": headers})
        return self._next()

    def _next(self) -> bytes:
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class HttpxHttp:
    """Stand-in for HttpClient that sends real httpx requests, so respx can check the wire format."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get(
        self, url: str, *, params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None
    ) -> bytes:
        response = await self._client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.content


# --------------------------------------------------------------------------- storefronts


def test_storefront_table_is_well_formed() -> None:
    assert len(STOREFRONTS) >= 40
    for country, storefront in STOREFRONTS.items():
        assert len(country) == 2
        assert country.isascii()
        assert country.islower()
        assert 143441 <= storefront <= 143600
    assert len(set(STOREFRONTS.values())) == len(STOREFRONTS), "storefront ids must be unique"


@pytest.mark.parametrize(
    ("country", "storefront"),
    [
        ("us", 143441),
        ("fr", 143442),
        ("de", 143443),
        ("gb", 143444),
        ("ca", 143455),
        ("au", 143460),
        ("jp", 143462),
        ("in", 143467),
        ("br", 143503),
    ],
)
def test_well_known_storefront_ids(country: str, storefront: int) -> None:
    assert STOREFRONTS[country] == storefront


# --------------------------------------------------------------------------- parse_hints


def test_parse_hints_xml_plist() -> None:
    assert parse_hints(HINTS_XML) == EXPECTED_HINTS


def test_parse_hints_binary_plist() -> None:
    binary = plistlib.dumps(plistlib.loads(HINTS_XML), fmt=plistlib.FMT_BINARY)
    assert binary.startswith(b"bplist")
    assert parse_hints(binary) == EXPECTED_HINTS


def test_parse_hints_json() -> None:
    raw = json.dumps({"hints": [{"term": t, "url": "https://example.invalid"} for t in EXPECTED_HINTS]})
    assert parse_hints(raw.encode()) == EXPECTED_HINTS


def test_parse_hints_tolerates_bom_and_leading_whitespace() -> None:
    assert parse_hints(b"\xef\xbb\xbf\n  " + HINTS_XML) == EXPECTED_HINTS
    assert parse_hints(b'\n\n{"hints": [{"term": "yoga"}]}') == ["yoga"]


def test_parse_hints_normalizes_and_deduplicates_in_order() -> None:
    raw = json.dumps(
        {
            "hints": [
                {"term": "  Meditation   App "},
                {"term": "yoga"},
                {"term": "MEDITATION app"},
                {"term": ""},
                {"term": "   "},
                {"url": "https://example.invalid/no-term"},
                {"term": 42},
                "stray string",
                {"term": "Yoga Nidra"},
            ]
        }
    ).encode()
    assert parse_hints(raw) == ["meditation app", "yoga", "yoga nidra"]


def test_parse_hints_empty_list() -> None:
    raw = plistlib.dumps({"title": "Suggestions", "hints": []})
    assert parse_hints(raw) == []


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"<!DOCTYPE html><html><body>Service Unavailable</body></html>", id="html-page"),
        pytest.param(b"Service Unavailable", id="plain-text"),
        pytest.param(HINTS_XML[:300], id="truncated-plist"),
        pytest.param(b"bplist00\x00\x01garbage", id="corrupt-binary-plist"),
        pytest.param(plistlib.dumps(["meditation"]), id="plist-array"),
        pytest.param(b'{"hints": [', id="truncated-json"),
        pytest.param(b"[" * 100_000, id="deeply-nested-json"),
        pytest.param(b'[{"term": "meditation"}]', id="json-array"),
        pytest.param(b'{"results": []}', id="no-hints-key"),
        pytest.param(b'{"hints": "meditation"}', id="hints-not-a-list"),
        pytest.param(b'{"hints": ["meditation", "meditation app"]}', id="hints-without-terms"),
    ],
)
def test_parse_hints_rejects_unrecognised_payloads(raw: bytes) -> None:
    with pytest.raises(StoreParseError):
        parse_hints(raw)


# --------------------------------------------------------------------------- parse_search


def test_parse_search_fixture() -> None:
    assert parse_search(SEARCH_JSON) == EXPECTED_APPS


def test_parse_search_empty() -> None:
    assert parse_search(b'\n\n\n{\n "resultCount":0,\n "results": []\n}\n\n\n') == []


def test_parse_search_skips_unusable_results_and_coerces_fields() -> None:
    raw = json.dumps(
        {
            "resultCount": 9,
            "results": [
                {"trackId": 1, "trackName": "One", "userRatingCount": -3, "averageUserRating": "4.5"},
                {"trackName": "No id"},
                {"trackId": 2},
                "not a dict",
                {"trackId": True, "trackName": "Bool id"},
                {"trackId": "²", "trackName": "Non-ASCII digit id"},
                {"trackId": "3", "trackName": "String id", "userRatingCount": 12.0, "averageUserRating": 3},
                {"trackId": 4, "trackName": "Four", "userRatingCount": "many", "artistName": None},
                {"trackId": 1, "trackName": "Duplicate of one"},
            ],
        }
    ).encode()
    assert parse_search(raw) == [
        AppSummary(app_id="1", title="One", developer="", ratings=0, rating=None, url=None),
        AppSummary(app_id="3", title="String id", ratings=12, rating=3.0),
        AppSummary(app_id="4", title="Four", developer="", ratings=0, rating=None),
    ]


def test_parse_search_ignores_non_finite_numbers() -> None:
    item = b'{"trackId": 9, "trackName": "X", "userRatingCount": NaN, "averageUserRating": Infinity}'
    raw = b'{"results": [' + item + b"]}"
    assert parse_search(raw) == [AppSummary(app_id="9", title="X", ratings=0, rating=None)]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"<html><body>Bad Gateway</body></html>", id="html-page"),
        pytest.param(b"[]", id="json-array"),
        pytest.param(b'{"resultCount": 0}', id="no-results-key"),
        pytest.param(b'{"results": {}}', id="results-not-a-list"),
        pytest.param(b'{"results": [{"wrapperType": "artist", "artistName": "X"}]}', id="no-apps"),
        pytest.param(b"[" * 100_000, id="deeply-nested-json"),
    ],
)
def test_parse_search_rejects_unrecognised_payloads(raw: bytes) -> None:
    with pytest.raises(StoreParseError):
        parse_search(raw)


# --------------------------------------------------------------------------- AppStoreClient


def test_client_implements_store_client() -> None:
    client = AppStoreClient(FakeHttp(), "us")  # type: ignore[arg-type]
    assert isinstance(client, StoreClient)
    assert client.store is Store.APPSTORE
    assert client.supports_difficulty is True


def test_unknown_country_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown App Store country 'zz'"):
        AppStoreClient(FakeHttp(), "zz")  # type: ignore[arg-type]


def test_country_is_case_insensitive() -> None:
    client = AppStoreClient(FakeHttp(), " GB ")  # type: ignore[arg-type]
    assert client.country == "gb"
    assert client.storefront == "143444-1,29"


@pytest.mark.parametrize(
    ("country", "header"),
    [("us", "143441-1,29"), ("gb", "143444-1,29"), ("de", "143443-1,29"), ("jp", "143462-1,29")],
)
async def test_suggest_request_shape(country: str, header: str) -> None:
    http = FakeHttp(HINTS_XML)
    client = AppStoreClient(http, country)  # type: ignore[arg-type]

    assert await client.suggest("meditation") == EXPECTED_HINTS
    assert http.calls == [
        {
            "method": "GET",
            "url": HINTS_URL,
            "params": {"clientApplication": "Software", "term": "meditation"},
            "headers": {"X-Apple-Store-Front": header},
        }
    ]


@pytest.mark.parametrize(
    ("language", "lang_param"),
    [("en", "en_us"), ("en-gb", "en_us"), ("ja", "ja_jp"), ("de", None), ("pt-br", None)],
)
async def test_top_apps_request_shape(language: str, lang_param: str | None) -> None:
    http = FakeHttp(SEARCH_JSON)
    client = AppStoreClient(http, "us", language)  # type: ignore[arg-type]

    assert await client.top_apps("meditation") == EXPECTED_APPS
    expected_params = {"term": "meditation", "country": "us", "entity": "software", "limit": "10"}
    if lang_param is not None:
        expected_params["lang"] = lang_param
    assert http.calls == [{"method": "GET", "url": SEARCH_URL, "params": expected_params, "headers": None}]


async def test_top_apps_respects_limit() -> None:
    http = FakeHttp(SEARCH_JSON)
    client = AppStoreClient(http, "gb")  # type: ignore[arg-type]

    assert await client.top_apps("meditation", limit=2) == EXPECTED_APPS[:2]
    assert http.calls[0]["params"]["limit"] == "2"
    assert http.calls[0]["params"]["country"] == "gb"


@pytest.mark.parametrize("limit", [0, -1, 201])
async def test_top_apps_rejects_out_of_range_limit(limit: int) -> None:
    http = FakeHttp()
    client = AppStoreClient(http, "us")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        await client.top_apps("meditation", limit=limit)
    assert http.calls == []


async def test_transport_errors_propagate_unchanged() -> None:
    error = StoreUnavailableError("search.itunes.apple.com: 503")
    client = AppStoreClient(FakeHttp(error), "us")  # type: ignore[arg-type]
    with pytest.raises(StoreUnavailableError) as excinfo:
        await client.suggest("meditation")
    assert excinfo.value is error


async def test_unrecognised_payload_raises_parse_error() -> None:
    client = AppStoreClient(FakeHttp(b"<html>maintenance</html>"), "us")  # type: ignore[arg-type]
    with pytest.raises(StoreParseError):
        await client.suggest("meditation")


async def test_suggest_wire_format() -> None:
    async with respx.mock(assert_all_called=True) as router, httpx.AsyncClient(trust_env=False) as http:
        route = router.get(HINTS_URL).respond(200, content=HINTS_XML, headers={"Content-Type": "text/xml"})
        client = AppStoreClient(HttpxHttp(http), "au")  # type: ignore[arg-type]
        assert await client.suggest("sleep sounds") == EXPECTED_HINTS

    request = route.calls.last.request
    assert request.method == "GET"
    assert str(request.url) == f"{HINTS_URL}?clientApplication=Software&term=sleep+sounds"
    assert request.headers["X-Apple-Store-Front"] == "143460-1,29"


async def test_top_apps_wire_format() -> None:
    async with respx.mock(assert_all_called=True) as router, httpx.AsyncClient(trust_env=False) as http:
        route = router.get(SEARCH_URL).respond(200, content=SEARCH_JSON)
        client = AppStoreClient(HttpxHttp(http), "jp", "ja")  # type: ignore[arg-type]
        assert await client.top_apps("瞑想", limit=5) == EXPECTED_APPS

    request = route.calls.last.request
    assert request.method == "GET"
    assert request.url.copy_with(query=None) == httpx.URL(SEARCH_URL)
    assert list(request.url.params.multi_items()) == [
        ("term", "瞑想"),
        ("country", "jp"),
        ("entity", "software"),
        ("limit", "5"),
        ("lang", "ja_jp"),
    ]
