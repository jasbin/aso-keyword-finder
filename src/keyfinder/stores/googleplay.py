"""Google Play client: search autocomplete via the Play web app's batchexecute RPC endpoint."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

from keyfinder.models import Store, normalize_term
from keyfinder.stores.base import StoreParseError

if TYPE_CHECKING:
    from keyfinder.http import HttpClient

logger = logging.getLogger(__name__)

SUGGEST_URL = "https://play.google.com/_/PlayStoreUi/data/batchexecute"
SUGGEST_RPC_ID = "IJ4APc"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded;charset=UTF-8"

# Anti-XSSI guard Google puts in front of JSON responses so they cannot be loaded as a <script>.
XSSI_PREFIX = ")]}'"

_COUNTRY_RE = re.compile(r"[a-z]{2}")
_NON_SPACE_RE = re.compile(r"\S")
_JSON = json.JSONDecoder()


def build_suggest_form(term: str) -> dict[str, str]:
    """Form body for the IJ4APc (search suggest) RPC.

    ``f.req`` is a JSON envelope ``[[[rpc_id, args_json, null, "generic"]]]`` whose args are
    themselves JSON-encoded: ``[[null, [term], [10], [2], 4]]`` (10 = number of suggestions).
    """
    args = json.dumps([[None, [term], [10], [2], 4]], separators=(",", ":"))
    envelope = json.dumps([[[SUGGEST_RPC_ID, args, None, "generic"]]], separators=(",", ":"))
    return {"f.req": envelope}


def _iter_json_values(text: str) -> Iterator[object]:
    """Yield consecutive whitespace-separated JSON values.

    batchexecute answers either with a single JSON array or, in its chunked form, with
    ``<length>\\n<json array>\\n`` repeated. The lengths are counted in UTF-16 code units by the
    server, so rather than slicing on them they are read as the plain JSON numbers they are.
    """
    pos = 0
    while (start := _NON_SPACE_RE.search(text, pos)) is not None:
        try:
            value, pos = _JSON.raw_decode(text, start.start())
        except (ValueError, RecursionError) as exc:
            raise StoreParseError(f"Google Play suggest response not recognised: malformed JSON ({exc})") from exc
        yield value


def _find_rpc_envelope(text: str, rpc_id: str) -> list[object] | None:
    """The first ``["wrb.fr", rpc_id, payload, ...]`` entry in any chunk, or None if there is none."""
    for chunk in _iter_json_values(text):
        if not isinstance(chunk, list):
            continue  # chunk length prefix
        for entry in chunk:
            if isinstance(entry, list) and len(entry) >= 3 and entry[:2] == ["wrb.fr", rpc_id]:
                return entry
    return None


def _suggestion_entries(data: object) -> list[object]:
    """``data[0][0]`` of the decoded payload; empty when the store has no suggestions."""
    what = "Google Play suggest payload not recognised"
    if data is None:
        return []
    if not isinstance(data, list):
        raise StoreParseError(f"{what}: top level is {type(data).__name__}, expected a list")
    if not data or data[0] is None:
        return []
    groups = data[0]
    if not isinstance(groups, list):
        raise StoreParseError(f"{what}: data[0] is {type(groups).__name__}, expected a list")
    if not groups or groups[0] is None:
        return []
    entries = groups[0]
    if not isinstance(entries, list):
        raise StoreParseError(f"{what}: data[0][0] is {type(entries).__name__}, expected a list")
    return entries


def parse_suggest(raw: bytes) -> list[str]:
    """Extract suggestion terms from a batchexecute IJ4APc response.

    Handles the ``)]}'`` XSSI prefix and both the single-array and the length-prefixed chunked
    response forms. The RPC payload is a JSON string; ``data[0][0][i][0]`` are the suggested terms.
    A null/empty payload means no suggestions. Terms are normalized and de-duplicated in display
    order. Raises StoreParseError when the response envelope or payload has an unknown shape.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StoreParseError(f"Google Play suggest response not recognised: not UTF-8 ({exc})") from exc
    text = text.lstrip().removeprefix(XSSI_PREFIX)
    envelope = _find_rpc_envelope(text, SUGGEST_RPC_ID)
    if envelope is None:
        raise StoreParseError(f"Google Play suggest response not recognised: no {SUGGEST_RPC_ID} envelope")
    payload = envelope[2]
    if payload is None or payload == "":
        if len(envelope) > 5 and envelope[5] is not None:
            logger.debug("Google Play %s returned no payload, status %r", SUGGEST_RPC_ID, envelope[5])
        return []
    if not isinstance(payload, str):
        raise StoreParseError(
            f"Google Play suggest payload not recognised: {type(payload).__name__}, expected a JSON string"
        )
    try:
        data = json.loads(payload)
    except (ValueError, RecursionError) as exc:
        raise StoreParseError(f"Google Play suggest payload not recognised: malformed JSON ({exc})") from exc
    entries = _suggestion_entries(data)
    terms = [e[0] for e in entries if isinstance(e, list) and e and isinstance(e[0], str)]
    if entries and not terms:
        raise StoreParseError(
            f"Google Play suggest payload not recognised: none of the {len(entries)} entries has a term"
        )
    return list(dict.fromkeys(t for t in map(normalize_term, terms) if t))


class GooglePlayClient:
    """StoreClient for Google Play in one country/language. Google Play exposes no rankings here."""

    store: Store = Store.PLAY
    supports_difficulty: bool = False

    def __init__(self, http: HttpClient, country: str, language: str = "en") -> None:
        country = country.strip().lower()
        language = language.strip()
        if not _COUNTRY_RE.fullmatch(country):
            raise ValueError(f"country must be a two-letter ISO code, got {country!r}")
        if not language:
            raise ValueError("language must not be empty")
        self._http = http
        self.country = country
        self.language = language

    async def suggest(self, term: str) -> list[str]:
        """Autocomplete suggestions Google Play shows while `term` is typed in the search box."""
        raw = await self._http.post(
            SUGGEST_URL,
            params={"rpcids": SUGGEST_RPC_ID, "hl": self.language, "gl": self.country},
            data=build_suggest_form(term),
            headers={"Content-Type": FORM_CONTENT_TYPE},
        )
        terms = parse_suggest(raw)
        logger.debug("play/%s suggestions for %r: %d", self.country, term, len(terms))
        return terms

    async def top_apps(self, term: str, limit: int = 10) -> None:
        """Google Play rankings are not available, so difficulty can't be scored for this store."""
        return None
