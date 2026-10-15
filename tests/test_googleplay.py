from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from keyfinder.models import Store
from keyfinder.stores.base import StoreClient, StoreParseError, StoreRateLimitedError
from keyfinder.stores.googleplay import SUGGEST_URL, GooglePlayClient, build_suggest_form, parse_suggest

FIXTURES = Path(__file__).parent / "fixtures"
CHUNKED = (FIXTURES / "play_suggest_chunked.txt").read_bytes()
SINGLE = (FIXTURES / "play_suggest_single.txt").read_bytes()

EXPECTED = [
    "photo editor",
    "photo editor free",
    "photo editor ai",
    "photo editor pro",
    "photo editor collage",
    "photo editor background remover",
    "photo editor for pictures",
    "photo editor app",
]


def batchexecute(payload: object, *, chunked: bool = True) -> bytes:
    """A batchexecute response carrying `payload` as the IJ4APc result, in either response form."""
    envelope = [["wrb.fr", "IJ4APc", payload, None, None, None, "generic"]]
    if not chunked:
        return (")]}'\n\n" + json.dumps(envelope)).encode()
    chunks = [json.dumps(envelope), json.dumps([["e", 4, None, None, 99]])]
    return (")]}'\n\n" + "".join(f"{len(c) + 1}\n{c}\n" for c in chunks)).encode()


def suggestions_payload(*terms: str) -> str:
    return json.dumps([[[[t] for t in terms]]])


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

    async def post(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        response = await self._client.post(url, params=params, data=data, headers=headers)
        response.raise_for_status()
        return response.content


# --------------------------------------------------------------------------- build_suggest_form


def test_build_suggest_form_structure() -> None:
    form = build_suggest_form("photo editor")
    assert list(form) == ["f.req"]
    envelope = json.loads(form["f.req"])
    assert envelope == [[["IJ4APc", envelope[0][0][1], None, "generic"]]]
    assert json.loads(envelope[0][0][1]) == [[None, ["photo editor"], [10], [2], 4]]


def test_build_suggest_form_exact_encoding() -> None:
    assert build_suggest_form("yoga") == {
        "f.req": '[[["IJ4APc","[[null,[\\"yoga\\"],[10],[2],4]]",null,"generic"]]]',
    }


@pytest.mark.parametrize("term", ['quote " and \\ backslash', "café ☕", "瞑想", "[[null]]"])
def test_build_suggest_form_round_trips_any_term(term: str) -> None:
    envelope = json.loads(build_suggest_form(term)["f.req"])
    assert json.loads(envelope[0][0][1])[0][1] == [term]


# --------------------------------------------------------------------------- parse_suggest


def test_parse_suggest_chunked_response() -> None:
    assert CHUNKED.startswith(b")]}'\n\n")
    assert parse_suggest(CHUNKED) == EXPECTED


def test_parse_suggest_single_array_response() -> None:
    assert parse_suggest(SINGLE) == EXPECTED


def test_parse_suggest_without_xssi_prefix() -> None:
    assert parse_suggest(SINGLE.removeprefix(b")]}'")) == EXPECTED
    assert parse_suggest(CHUNKED.removeprefix(b")]}'")) == EXPECTED


def test_parse_suggest_crlf_line_endings() -> None:
    assert parse_suggest(CHUNKED.replace(b"\n", b"\r\n")) == EXPECTED


def test_parse_suggest_finds_envelope_in_a_later_chunk() -> None:
    first = json.dumps([["di", 12], ["af.httprm", 11, "-1", 21]])
    second = json.dumps([["wrb.fr", "IJ4APc", suggestions_payload("yoga"), None, None, None, "generic"]])
    raw = f")]}}'\n\n{len(first) + 1}\n{first}\n{len(second) + 1}\n{second}\n".encode()
    assert parse_suggest(raw) == ["yoga"]


def test_parse_suggest_ignores_other_rpc_ids() -> None:
    raw = (
        ")]}'\n\n"
        + json.dumps(
            [
                ["wrb.fr", "OtherRpc", suggestions_payload("wrong"), None, None, None, "generic"],
                ["wrb.fr", "IJ4APc", suggestions_payload("right"), None, None, None, "generic"],
            ]
        )
    ).encode()
    assert parse_suggest(raw) == ["right"]


def test_parse_suggest_normalizes_and_deduplicates_in_order() -> None:
    entries = [["  Photo   Editor "], ["collage maker", [["extra"]]], ["PHOTO editor"], [""], [], None, [7]]
    payload = json.dumps([[[*entries, ["Retouch"]]]])
    assert parse_suggest(batchexecute(payload)) == ["photo editor", "collage maker", "retouch"]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="null-payload"),
        pytest.param("", id="empty-payload"),
        pytest.param("null", id="json-null"),
        pytest.param("[]", id="empty-list"),
        pytest.param("[null]", id="null-group"),
        pytest.param("[[]]", id="empty-group"),
        pytest.param("[[null]]", id="null-entries"),
        pytest.param("[[[]]]", id="no-entries"),
    ],
)
@pytest.mark.parametrize("chunked", [True, False], ids=["chunked", "single"])
def test_parse_suggest_no_suggestions(payload: str | None, chunked: bool) -> None:
    assert parse_suggest(batchexecute(payload, chunked=chunked)) == []


def test_parse_suggest_rpc_error_status_means_no_suggestions() -> None:
    raw = (")]}'\n\n" + json.dumps([["wrb.fr", "IJ4APc", None, None, None, [3], "generic"]])).encode()
    assert parse_suggest(raw) == []


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b")]}'\n\n", id="prefix-only"),
        pytest.param(b"<!DOCTYPE html><html><body>Error 404</body></html>", id="html-page"),
        pytest.param(b"\xff\xfe\x00garbage", id="not-utf8"),
        pytest.param(b')]}\'\n\n25\n[["e",4,null,null,25]]\n', id="no-rpc-envelope"),
        pytest.param(b')]}\'\n\n[["wrb.fr","IJ4APc"', id="truncated-envelope"),
        pytest.param(b")]}'\n\n" + b"[" * 100_000, id="deeply-nested"),
        pytest.param(batchexecute('[[[["photo'), id="payload-not-json"),
        pytest.param(batchexecute(42), id="payload-not-a-string"),
        pytest.param(batchexecute('{"terms": ["photo editor"]}'), id="payload-is-object"),
        pytest.param(batchexecute('["photo editor"]'), id="data0-not-a-list"),
        pytest.param(batchexecute('[["photo editor"]]'), id="data00-not-a-list"),
        pytest.param(batchexecute('[[[{"term": "photo editor"}]]]'), id="entries-without-terms"),
    ],
)
def test_parse_suggest_rejects_unrecognised_payloads(raw: bytes) -> None:
    with pytest.raises(StoreParseError):
        parse_suggest(raw)


# --------------------------------------------------------------------------- GooglePlayClient


def test_client_implements_store_client() -> None:
    client = GooglePlayClient(FakeHttp(), "us")  # type: ignore[arg-type]
    assert isinstance(client, StoreClient)
    assert client.store is Store.PLAY
    assert client.supports_difficulty is False


@pytest.mark.parametrize(("country", "language"), [("usa", "en"), ("", "en"), ("u1", "en"), ("us", " ")])
def test_invalid_country_or_language_is_rejected(country: str, language: str) -> None:
    with pytest.raises(ValueError):
        GooglePlayClient(FakeHttp(), country, language)  # type: ignore[arg-type]


async def test_suggest_request_shape() -> None:
    http = FakeHttp(CHUNKED)
    client = GooglePlayClient(http, " DE ", "de")  # type: ignore[arg-type]

    assert await client.suggest("photo editor") == EXPECTED
    assert http.calls == [
        {
            "method": "POST",
            "url": SUGGEST_URL,
            "params": {"rpcids": "IJ4APc", "hl": "de", "gl": "de"},
            "data": build_suggest_form("photo editor"),
            "headers": {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        }
    ]


async def test_top_apps_is_unsupported_and_makes_no_request() -> None:
    http = FakeHttp()
    client: StoreClient = GooglePlayClient(http, "us")  # type: ignore[arg-type]
    assert await client.top_apps("photo editor") is None
    assert await client.top_apps("photo editor", limit=50) is None
    assert http.calls == []


async def test_transport_errors_propagate_unchanged() -> None:
    error = StoreRateLimitedError("play.google.com: 429")
    client = GooglePlayClient(FakeHttp(error), "us")  # type: ignore[arg-type]
    with pytest.raises(StoreRateLimitedError) as excinfo:
        await client.suggest("photo editor")
    assert excinfo.value is error


async def test_suggest_wire_format() -> None:
    async with respx.mock(assert_all_called=True) as router, httpx.AsyncClient(trust_env=False) as http:
        route = router.post(SUGGEST_URL).respond(
            200, content=CHUNKED, headers={"Content-Type": "application/json; charset=utf-8"}
        )
        client = GooglePlayClient(HttpxHttp(http), "gb", "en-gb")  # type: ignore[arg-type]
        assert await client.suggest("photo editor") == EXPECTED

    request = route.calls.last.request
    assert request.method == "POST"
    assert str(request.url) == f"{SUGGEST_URL}?rpcids=IJ4APc&hl=en-gb&gl=gb"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded;charset=UTF-8"
    form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
    assert form == {"f.req": ['[[["IJ4APc","[[null,[\\"photo editor\\"],[10],[2],4]]",null,"generic"]]]']}
