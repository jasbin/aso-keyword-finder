from __future__ import annotations

import asyncio
import email.utils
import itertools
import logging
import urllib.parse
from collections import defaultdict
from typing import Any

import httpx
import pytest
import respx

import keyfinder.http as http_module
from keyfinder.config import Settings
from keyfinder.http import HttpClient, RequestStats
from keyfinder.stores.base import StoreRateLimitedError, StoreUnavailableError

URL = "https://api.example.com/search"
OTHER_URL = "https://other.example.org/data"
HOST = "api.example.com"
WALL_EPOCH = 1_700_000_000

_real_sleep = asyncio.sleep


class FakeClock:
    """Replaces ``time`` inside keyfinder.http and ``asyncio.sleep``: sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return WALL_EPOCH + self.now

    async def sleep(self, delay: float, result: Any = None) -> Any:
        self.sleeps.append(delay)
        self.now += max(delay, 0.0)
        await _real_sleep(0)
        return result


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(http_module, "time", fake)
    monkeypatch.setattr(asyncio, "sleep", fake.sleep)
    return fake


def client(settings: Settings, **overrides: Any) -> HttpClient:
    return HttpClient(settings.model_copy(update=overrides))


def assert_backoff(delay: float, attempt: int) -> None:
    base = min(30.0, 0.5 * 2**attempt)
    assert base <= delay <= base + 0.25


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
async def test_get_returns_body_and_sends_headers(settings: Settings, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, content=b"payload"))

    async with HttpClient(settings) as http:
        body = await http.get(
            URL, params={"term": "yoga mat"}, headers={"X-Apple-Store-Front": "143441-1,29"}
        )

    assert body == b"payload"
    request = route.calls.last.request
    assert request.headers["User-Agent"] == settings.user_agent
    assert request.headers["X-Apple-Store-Front"] == "143441-1,29"
    assert request.url.params["term"] == "yoga mat"
    assert http.stats == RequestStats(requests=1, retries=0, failures=0)


async def test_post_sends_form_encoded_data(settings: Settings, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(URL).mock(return_value=httpx.Response(200, content=b")]}'\n[]"))

    async with HttpClient(settings) as http:
        body = await http.post(URL, params={"rpcids": "IJ4APc", "hl": "en"}, data={"f.req": '[["x", 1]]'})

    assert body == b")]}'\n[]"
    request = route.calls.last.request
    assert request.method == "POST"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs(request.content.decode()) == {"f.req": ['[["x", 1]]']}
    assert dict(request.url.params) == {"rpcids": "IJ4APc", "hl": "en"}


async def test_follows_redirects(settings: Settings, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(return_value=httpx.Response(302, headers={"Location": OTHER_URL}))
    respx_mock.get(OTHER_URL).mock(return_value=httpx.Response(200, content=b"moved"))

    async with HttpClient(settings) as http:
        assert await http.get(URL) == b"moved"


async def test_uses_custom_transport(settings: Settings) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"mocked")

    async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
        assert await http.get(URL, params={"a": "1"}) == b"mocked"
    assert seen == [f"{URL}?a=1"]


# --------------------------------------------------------------------------- #
# Retries and backoff
# --------------------------------------------------------------------------- #
async def test_retries_server_error_then_succeeds(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, content=b"ok")])

    async with HttpClient(settings) as http:
        assert await http.get(URL) == b"ok"

    assert http.stats == RequestStats(requests=2, retries=1, failures=0)
    (delay,) = clock.sleeps
    assert_backoff(delay, 0)


async def test_exponential_backoff_then_unavailable(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = respx_mock.get(URL).mock(return_value=httpx.Response(500))
    http = client(settings, max_retries=3)

    with pytest.raises(StoreUnavailableError) as excinfo:
        await http.get(URL, params={"term": "secret plans"})
    await http.aclose()

    assert type(excinfo.value) is StoreUnavailableError
    message = str(excinfo.value)
    assert HOST in message
    assert "500" in message
    assert "secret" not in message
    assert "term=" not in message
    assert route.call_count == 4
    assert len(clock.sleeps) == 3
    for attempt, delay in enumerate(clock.sleeps):
        assert_backoff(delay, attempt)
    assert http.stats == RequestStats(requests=4, retries=3, failures=1)


async def test_backoff_is_capped(settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock) -> None:
    respx_mock.get(URL).mock(return_value=httpx.Response(502))
    http = client(settings, max_retries=10)

    with pytest.raises(StoreUnavailableError):
        await http.get(URL)
    await http.aclose()

    assert len(clock.sleeps) == 10
    for attempt, delay in enumerate(clock.sleeps):
        assert_backoff(delay, attempt)
    assert max(clock.sleeps) <= 30.25
    assert min(clock.sleeps[6:]) >= 30.0


async def test_no_retries_when_max_retries_is_zero(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(return_value=httpx.Response(503))
    http = client(settings, max_retries=0)

    with pytest.raises(StoreUnavailableError, match="1 attempt\\)"):
        await http.get(URL)
    await http.aclose()

    assert clock.sleeps == []
    assert http.stats == RequestStats(requests=1, retries=0, failures=1)


async def test_rate_limited_after_retries(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    route = respx_mock.get(URL).mock(return_value=httpx.Response(429))
    http = client(settings, max_retries=2)

    with pytest.raises(StoreRateLimitedError) as excinfo:
        await http.get(URL, params={"term": "secret"})
    await http.aclose()

    assert isinstance(excinfo.value, StoreUnavailableError)
    assert HOST in str(excinfo.value)
    assert "429" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)
    assert route.call_count == 3
    assert http.stats == RequestStats(requests=3, retries=2, failures=1)


async def test_rate_limit_that_clears_up_succeeds(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(side_effect=[httpx.Response(429), httpx.Response(200, content=b"ok")])
    async with HttpClient(settings) as http:
        assert await http.get(URL) == b"ok"
    assert http.stats == RequestStats(requests=2, retries=1, failures=0)


@pytest.mark.parametrize(
    ("status", "header", "expected"),
    [
        (429, "7", 7.0),
        (503, "2.5", 2.5),
        (429, "3600", 60.0),  # capped
        (429, "0", 0.0),
        (429, "-5", 0.0),
    ],
)
async def test_retry_after_seconds_is_honoured(
    settings: Settings,
    respx_mock: respx.MockRouter,
    clock: FakeClock,
    status: int,
    header: str,
    expected: float,
) -> None:
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(status, headers={"Retry-After": header}), httpx.Response(200)]
    )
    async with HttpClient(settings) as http:
        await http.get(URL)
    assert clock.sleeps == [expected]


@pytest.mark.parametrize("usegmt", [True, False], ids=["GMT", "-0000"])
@pytest.mark.parametrize(("offset", "expected"), [(12, 12.0), (-30, 0.0), (3600, 60.0)])
async def test_retry_after_http_date_is_honoured(
    settings: Settings,
    respx_mock: respx.MockRouter,
    clock: FakeClock,
    offset: int,
    expected: float,
    usegmt: bool,
) -> None:
    when = email.utils.formatdate(clock.time() + offset, usegmt=usegmt)
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": when}), httpx.Response(200)]
    )
    async with HttpClient(settings) as http:
        await http.get(URL)
    assert clock.sleeps == [pytest.approx(expected)]


@pytest.mark.parametrize("header", ["soon", "", "nan", "inf", "Mon, 99 Foo 2024"])
async def test_invalid_retry_after_falls_back_to_backoff(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock, header: str
) -> None:
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(503, headers={"Retry-After": header}), httpx.Response(200)]
    )
    async with HttpClient(settings) as http:
        await http.get(URL)
    (delay,) = clock.sleeps
    assert_backoff(delay, 0)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 304])
async def test_other_client_errors_fail_immediately(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock, status: int
) -> None:
    route = respx_mock.get(URL).mock(return_value=httpx.Response(status))
    http = client(settings, max_retries=3)

    with pytest.raises(StoreUnavailableError) as excinfo:
        await http.get(URL, params={"term": "secret"})
    await http.aclose()

    assert type(excinfo.value) is StoreUnavailableError
    assert HOST in str(excinfo.value)
    assert str(status) in str(excinfo.value)
    assert "secret" not in str(excinfo.value)
    assert route.call_count == 1
    assert clock.sleeps == []
    assert http.stats == RequestStats(requests=1, retries=0, failures=1)


# --------------------------------------------------------------------------- #
# Transport failures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("All connection attempts failed"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.ProxyError("403 Forbidden"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    ],
    ids=lambda exc: type(exc).__name__,
)
async def test_transport_errors_are_retried_then_unavailable(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock, error: httpx.TransportError
) -> None:
    route = respx_mock.get(URL).mock(side_effect=error)

    async with HttpClient(settings) as http:
        with pytest.raises(StoreUnavailableError) as excinfo:
            await http.get(URL, params={"term": "secret"})

    assert type(excinfo.value) is StoreUnavailableError
    assert isinstance(excinfo.value.__cause__, httpx.TransportError)
    message = str(excinfo.value)
    assert HOST in message
    assert type(error).__name__ in message
    assert "secret" not in message
    assert route.call_count == 2
    assert len(clock.sleeps) == 1
    assert http.stats == RequestStats(requests=2, retries=1, failures=1)


async def test_transport_error_then_success(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(
        side_effect=[httpx.ReadTimeout("timed out"), httpx.Response(200, content=b"recovered")]
    )
    async with HttpClient(settings) as http:
        assert await http.get(URL) == b"recovered"
    assert http.stats == RequestStats(requests=2, retries=1, failures=0)


async def test_query_strings_are_scrubbed_from_exception_details(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(
        side_effect=httpx.ConnectError(f"cannot reach {URL}?term=secret+plans&country=us (refused)")
    )
    async with HttpClient(settings) as http:
        with pytest.raises(StoreUnavailableError) as excinfo:
            await http.get(URL, params={"term": "secret plans"})

    message = str(excinfo.value)
    assert "secret" not in message
    assert "country=us" not in message
    assert f"{URL} (refused)" in message


async def test_long_exception_details_are_truncated(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(side_effect=httpx.ConnectError("x" * 5000))
    async with HttpClient(settings) as http:
        with pytest.raises(StoreUnavailableError) as excinfo:
            await http.get(URL)
    assert len(str(excinfo.value)) < 300
    assert "…" in str(excinfo.value)


async def test_redirect_loop_fails_without_retry(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(return_value=httpx.Response(302, headers={"Location": URL}))
    http = client(settings, max_retries=3)

    with pytest.raises(StoreUnavailableError, match="TooManyRedirects") as excinfo:
        await http.get(URL)
    await http.aclose()

    assert HOST in str(excinfo.value)
    assert clock.sleeps == []
    assert http.stats == RequestStats(requests=1, retries=0, failures=1)


# --------------------------------------------------------------------------- #
# Concurrency and politeness
# --------------------------------------------------------------------------- #
async def test_global_concurrency_limit(settings: Settings) -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        for _ in range(5):
            await _real_sleep(0)
        in_flight -= 1
        return httpx.Response(200, content=request.url.host.encode())

    hosts = [f"https://h{i % 3}.example.com/x" for i in range(9)]
    transport = httpx.MockTransport(handler)
    async with HttpClient(settings.model_copy(update={"max_concurrency": 2}), transport=transport) as http:
        bodies = await asyncio.gather(*(http.get(url) for url in hosts))

    assert bodies == [httpx.URL(url).host.encode() for url in hosts]
    assert peak == 2
    assert http.stats.requests == 9


async def test_backoff_sleep_does_not_hold_concurrency_slot(
    settings: Settings, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    backoff_started = asyncio.Event()
    other_done = asyncio.Event()

    async def parked_sleep(delay: float, result: Any = None) -> Any:
        # Park the retrying request until the other request has finished. If the backoff
        # held the only concurrency slot, the other request could never run: timeout.
        backoff_started.set()
        await asyncio.wait_for(other_done.wait(), timeout=5)
        return result

    monkeypatch.setattr(asyncio, "sleep", parked_sleep)
    respx_mock.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, content=b"first")])
    respx_mock.get(OTHER_URL).mock(return_value=httpx.Response(200, content=b"second"))

    async with HttpClient(settings.model_copy(update={"max_concurrency": 1})) as http:

        async def second() -> bytes:
            await backoff_started.wait()
            body = await http.get(OTHER_URL)
            other_done.set()
            return body

        results = await asyncio.gather(http.get(URL), second())

    assert list(results) == [b"first", b"second"]
    assert http.stats == RequestStats(requests=3, retries=1, failures=0)


async def test_per_host_spacing_between_request_starts(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    starts: dict[str, list[float]] = defaultdict(list)

    def record(request: httpx.Request) -> httpx.Response:
        starts[request.url.host].append(clock.now)
        return httpx.Response(200)

    respx_mock.get(URL).mock(side_effect=record)
    respx_mock.get(OTHER_URL).mock(side_effect=record)

    async with client(settings, request_interval=1.0, max_concurrency=8) as http:
        await asyncio.gather(*(http.get(URL) for _ in range(4)), http.get(OTHER_URL))

    same_host = starts[HOST]
    assert len(same_host) == 4
    gaps = [later - earlier for earlier, later in itertools.pairwise(same_host)]
    assert all(gap >= 1.0 for gap in gaps), gaps
    # Only the 3 follow-up requests to the busy host waited; the other host never did.
    assert clock.sleeps == [pytest.approx(1.0)] * 3
    assert len(starts["other.example.org"]) == 1


async def test_spacing_also_applies_to_retries(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    starts: list[float] = []
    responses = iter([httpx.Response(503), httpx.Response(200)])

    def record(request: httpx.Request) -> httpx.Response:
        starts.append(clock.now)
        return next(responses)

    respx_mock.get(URL).mock(side_effect=record)

    async with client(settings, request_interval=1.0) as http:
        await http.get(URL)

    backoff, spacing = clock.sleeps
    assert_backoff(backoff, 0)
    assert backoff + spacing == pytest.approx(1.0)
    assert starts[1] - starts[0] == pytest.approx(1.0)


async def test_zero_interval_never_sleeps(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(return_value=httpx.Response(200))
    async with client(settings, request_interval=0) as http:
        await asyncio.gather(*(http.get(URL) for _ in range(5)))
    assert clock.sleeps == []


# --------------------------------------------------------------------------- #
# Lifecycle, cancellation, logging
# --------------------------------------------------------------------------- #
async def test_cancellation_during_backoff_propagates_and_frees_slot(
    settings: Settings, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_backoff = asyncio.Event()

    async def endless_sleep(delay: float, result: Any = None) -> Any:
        in_backoff.set()
        await asyncio.Event().wait()
        return result

    monkeypatch.setattr(asyncio, "sleep", endless_sleep)
    respx_mock.get(URL).mock(return_value=httpx.Response(503))
    respx_mock.get(OTHER_URL).mock(return_value=httpx.Response(200, content=b"ok"))

    async with client(settings, max_concurrency=1) as http:
        task = asyncio.create_task(http.get(URL))
        await in_backoff.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await http.get(OTHER_URL) == b"ok"

    assert http.stats == RequestStats(requests=2, retries=1, failures=0)


async def test_closed_client_refuses_requests(settings: Settings) -> None:
    http = HttpClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    async with http:
        assert await http.get(URL) == b""
    with pytest.raises(RuntimeError):
        await http.get(URL)


async def test_logs_name_host_but_never_the_query_string(
    settings: Settings, respx_mock: respx.MockRouter, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="keyfinder.http")
    respx_mock.get(URL).mock(return_value=httpx.Response(503))

    async with HttpClient(settings) as http:
        with pytest.raises(StoreUnavailableError):
            await http.get(f"{URL}?lang=en_us", params={"term": "secret"})

    records = [r for r in caplog.records if r.name == "keyfinder.http"]
    levels = [r.levelname for r in records]
    assert levels == ["DEBUG", "INFO", "DEBUG", "WARNING"]
    for record in records:
        assert "secret" not in record.getMessage()
        assert "lang=" not in record.getMessage()
        assert "secret" not in repr(vars(record))
        assert getattr(record, "host", None) == HOST
    assert "retrying GET api.example.com/search" in records[1].getMessage()
