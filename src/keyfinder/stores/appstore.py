"""Apple App Store client: search hints (autocomplete) and iTunes Search API rankings."""

from __future__ import annotations

import json
import logging
import math
import plistlib
from collections.abc import Mapping
from typing import TYPE_CHECKING

from keyfinder.models import AppSummary, Store, normalize_term
from keyfinder.stores.base import StoreParseError

if TYPE_CHECKING:
    from keyfinder.http import HttpClient

logger = logging.getLogger(__name__)

HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"
SEARCH_URL = "https://itunes.apple.com/search"

STOREFRONT_HEADER = "X-Apple-Store-Front"
# The X-Apple-Store-Front header has the form "<storefront id>-<language id>,<platform id>".
# Only the storefront id selects the country (whose catalogue and search hints are returned).
# "-1" is Apple's language id 1, the default language variant (Apple falls back to the storefront's
# own default where it differs), and ",29" is the platform/client id sent by current App Store clients,
# which makes MZSearchHints answer with App Store (software) hints. Every country uses the same "-1,29".
STOREFRONT_SUFFIX = "-1,29"

# ISO 3166-1 alpha-2 country -> Apple storefront id. Only well-established ids are listed: a wrong id
# does not fail, it silently returns another country's data, so add entries only when certain.
STOREFRONTS: dict[str, int] = {
    "us": 143441,  # United States
    "fr": 143442,  # France
    "de": 143443,  # Germany
    "gb": 143444,  # United Kingdom
    "at": 143445,  # Austria
    "be": 143446,  # Belgium
    "fi": 143447,  # Finland
    "gr": 143448,  # Greece
    "ie": 143449,  # Ireland
    "it": 143450,  # Italy
    "lu": 143451,  # Luxembourg
    "nl": 143452,  # Netherlands
    "pt": 143453,  # Portugal
    "es": 143454,  # Spain
    "ca": 143455,  # Canada
    "se": 143456,  # Sweden
    "no": 143457,  # Norway
    "dk": 143458,  # Denmark
    "ch": 143459,  # Switzerland
    "au": 143460,  # Australia
    "nz": 143461,  # New Zealand
    "jp": 143462,  # Japan
    "hk": 143463,  # Hong Kong
    "sg": 143464,  # Singapore
    "cn": 143465,  # China mainland
    "kr": 143466,  # South Korea
    "in": 143467,  # India
    "mx": 143468,  # Mexico
    "ru": 143469,  # Russia
    "tw": 143470,  # Taiwan
    "vn": 143471,  # Vietnam
    "za": 143472,  # South Africa
    "my": 143473,  # Malaysia
    "ph": 143474,  # Philippines
    "th": 143475,  # Thailand
    "id": 143476,  # Indonesia
    "pk": 143477,  # Pakistan
    "pl": 143478,  # Poland
    "sa": 143479,  # Saudi Arabia
    "tr": 143480,  # Turkey
    "ae": 143481,  # United Arab Emirates
    "hu": 143482,  # Hungary
    "cl": 143483,  # Chile
    "np": 143484,  # Nepal
    "pa": 143485,  # Panama
    "lk": 143486,  # Sri Lanka
    "ro": 143487,  # Romania
    "cz": 143489,  # Czechia
    "il": 143491,  # Israel
    "ua": 143492,  # Ukraine
    "kw": 143493,  # Kuwait
    "hr": 143494,  # Croatia
    "sk": 143496,  # Slovakia
    "qa": 143498,  # Qatar
    "si": 143499,  # Slovenia
    "co": 143501,  # Colombia
    "br": 143503,  # Brazil
    "ar": 143505,  # Argentina
    "pe": 143507,  # Peru
    "eg": 143516,  # Egypt
}

# The iTunes Search API documents only two result languages (`lang`): English and Japanese.
# Any other language leaves the parameter out, so Apple uses the storefront's default.
_SEARCH_LANGS: dict[str, str] = {"en": "en_us", "ja": "ja_jp"}

MAX_SEARCH_LIMIT = 200  # the Search API's documented maximum for `limit`

_UTF8_BOM = b"\xef\xbb\xbf"


def _load_document(raw: bytes, what: str) -> object:
    """Decode a plist (XML or binary) or JSON document; anything else raises StoreParseError."""
    body = raw.lstrip()
    if body.startswith(_UTF8_BOM):
        body = body[len(_UTF8_BOM) :].lstrip()
    preview = body[:40]
    if body.startswith((b"bplist", b"<")):
        try:
            return plistlib.loads(body)
        except Exception as exc:  # plistlib raises an undocumented mix (ExpatError, ValueError, ...)
            raise StoreParseError(f"{what}: not a valid plist ({exc}): {preview!r}") from exc
    if body.startswith((b"{", b"[")):
        try:
            return json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise StoreParseError(f"{what}: malformed JSON ({exc})") from exc
    raise StoreParseError(f"{what}: expected a plist or JSON document, got {preview!r}")


def parse_hints(raw: bytes) -> list[str]:
    """Extract suggestion terms from an MZSearchHints response.

    Accepts the XML or binary plist the endpoint returns (a dict whose ``hints`` array holds
    dicts with ``term`` and ``url``) and the equivalent JSON object. Terms are normalized and
    de-duplicated with their display order kept. Raises StoreParseError when the payload does not
    have that shape (e.g. an HTML error page or a changed endpoint).
    """
    what = "App Store hints response not recognised"
    doc = _load_document(raw, what)
    if not isinstance(doc, Mapping):
        raise StoreParseError(f"{what}: top level is {type(doc).__name__}, expected a dict")
    hints = doc.get("hints")
    if not isinstance(hints, list):
        raise StoreParseError(f"{what}: no 'hints' list")
    terms = [h["term"] for h in hints if isinstance(h, Mapping) and isinstance(h.get("term"), str)]
    if hints and not terms:
        raise StoreParseError(f"{what}: none of the {len(hints)} hints has a 'term'")
    return list(dict.fromkeys(t for t in map(normalize_term, terms) if t))


def _app_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value
    return None


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return max(int(value), 0)


def _rating(value: object) -> float | None:
    """Average star rating (1..5); Apple reports 0 for apps nobody has rated yet, which is None here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    rating = float(value)
    return rating if math.isfinite(rating) and rating > 0 else None


def parse_search(raw: bytes) -> list[AppSummary]:
    """Convert an iTunes Search API response into AppSummary objects, in ranking order.

    Results without a usable ``trackId``/``trackName`` are skipped; duplicate apps keep their
    first (best) position. Raises StoreParseError when the payload is not a Search API response.
    """
    what = "iTunes Search response not recognised"
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise StoreParseError(f"{what}: malformed JSON ({exc})") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("results"), list):
        raise StoreParseError(f"{what}: no 'results' list")
    results: list[object] = doc["results"]
    apps: dict[str, AppSummary] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        app_id = _app_id(item.get("trackId"))
        title = item.get("trackName")
        if app_id is None or not isinstance(title, str) or app_id in apps:
            continue
        developer = item.get("artistName")
        url = item.get("trackViewUrl")
        apps[app_id] = AppSummary(
            app_id=app_id,
            title=title,
            developer=developer if isinstance(developer, str) else "",
            ratings=_count(item.get("userRatingCount")),
            rating=_rating(item.get("averageUserRating")),
            url=url if isinstance(url, str) else None,
        )
    if results and not apps:
        raise StoreParseError(f"{what}: none of the {len(results)} results is an app")
    return list(apps.values())


def _search_lang(language: str) -> str | None:
    """Map a language tag such as 'en' or 'en-gb' to the Search API `lang` value, if it has one."""
    primary = language.strip().lower().replace("_", "-").split("-", 1)[0]
    return _SEARCH_LANGS.get(primary)


class AppStoreClient:
    """StoreClient for the Apple App Store of one country."""

    store: Store = Store.APPSTORE
    supports_difficulty: bool = True

    def __init__(self, http: HttpClient, country: str, language: str = "en") -> None:
        country = country.strip().lower()
        if country not in STOREFRONTS:
            raise ValueError(
                f"unknown App Store country {country!r}; supported: {', '.join(sorted(STOREFRONTS))}"
            )
        self._http = http
        self.country = country
        self.language = language
        self.storefront = f"{STOREFRONTS[country]}{STOREFRONT_SUFFIX}"

    async def suggest(self, term: str) -> list[str]:
        """Search hints Apple shows while `term` is typed in the App Store search field."""
        raw = await self._http.get(
            HINTS_URL,
            params={"clientApplication": "Software", "term": term},
            headers={STOREFRONT_HEADER: self.storefront},
        )
        terms = parse_hints(raw)
        logger.debug("appstore/%s hints for %r: %d", self.country, term, len(terms))
        return terms

    async def top_apps(self, term: str, limit: int = 10) -> list[AppSummary]:
        """The top `limit` (1..200) apps ranking for `term`, best first."""
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}, got {limit}")
        params = {"term": term, "country": self.country, "entity": "software", "limit": str(limit)}
        lang = _search_lang(self.language)
        if lang is not None:
            params["lang"] = lang
        apps = parse_search(await self._http.get(SEARCH_URL, params=params))[:limit]
        logger.debug("appstore/%s top apps for %r: %d", self.country, term, len(apps))
        return apps
