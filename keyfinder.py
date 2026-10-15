#!/usr/bin/env python3
"""ASO keyword finder.

Expands a seed keyword through App Store / Google Play autocomplete, then scores
each suggestion:

  popularity  - how early the term surfaces while typing (shortest prefix that
                shows it, and its position in that list). A relative proxy,
                NOT a search count.
  difficulty  - App Store only: strength of the top 10 apps ranking for the
                term (median rating count) and how many target it in the title.
  opportunity - popularity discounted by difficulty.

Standard library only. Usage:
  python3 keyfinder.py research "meditation" --store appstore --country us
  python3 keyfinder.py serve            # web UI on http://127.0.0.1:8000
"""
import argparse
import csv
import json
import math
import plistlib
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# App Store storefront ids, sent in the X-Apple-Store-Front header.
STOREFRONTS = {
    "us": 143441, "gb": 143444, "ca": 143455, "au": 143460, "in": 143467,
    "de": 143443, "fr": 143442, "es": 143454, "it": 143450, "jp": 143462,
    "br": 143503, "mx": 143468, "nl": 143452, "se": 143456, "kr": 143466,
    "np": 143484, "sg": 143464, "ae": 143481, "nz": 143461, "ie": 143449,
}

APPLE_HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
PLAY_SUGGEST_URL = "https://play.google.com/_/PlayStoreUi/data/batchexecute"


def normalize(term):
    return " ".join(term.lower().split())


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Fetcher:
    """Polite HTTP client: fixed delay between requests, small retry budget."""

    def __init__(self, delay=0.3, timeout=15, retries=2):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0
        self.requests = 0

    def fetch(self, url, data=None, headers=None):
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        body = data.encode() if isinstance(data, str) else data
        for attempt in range(self.retries + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                req = urllib.request.Request(url, data=body, headers=hdrs)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == self.retries:
                    raise RuntimeError(f"request failed: {url[:80]}... ({exc})") from exc
                time.sleep(1.5 * (attempt + 1))


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #
def parse_apple_hints(raw):
    """MZSearchHints returns a plist (older) or JSON; both carry hints[].term."""
    try:
        data = plistlib.loads(raw)
    except Exception:
        data = json.loads(raw)
    return [h["term"] for h in data.get("hints", []) if h.get("term")]


def parse_itunes_search(raw):
    results = json.loads(raw).get("results", [])
    return [
        {
            "title": r.get("trackName", ""),
            "developer": r.get("artistName", ""),
            "ratings": int(r.get("userRatingCount") or 0),
            "rating": round(float(r.get("averageUserRating") or 0), 2),
        }
        for r in results
    ]


def parse_play_suggest(raw):
    """batchexecute replies with an XSSI prefix, then a JSON envelope whose
    IJ4APc entry holds another JSON string with the suggestion list."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    text = text[text.index("["):]
    for entry in json.loads(text):
        if len(entry) > 2 and entry[0] == "wrb.fr" and entry[1] == "IJ4APc":
            if not entry[2]:
                return []
            data = json.loads(entry[2])
            if not data or not data[0] or not data[0][0]:
                return []
            return [s[0] for s in data[0][0] if s and s[0]]
    return []


class AppStore:
    name = "appstore"
    supports_difficulty = True

    def __init__(self, fetcher, country="us"):
        if country not in STOREFRONTS:
            raise ValueError(f"unknown App Store country '{country}', known: {', '.join(sorted(STOREFRONTS))}")
        self.fetcher = fetcher
        self.country = country
        self.storefront = f"{STOREFRONTS[country]}-1,29"

    def suggest(self, term):
        qs = urllib.parse.urlencode({"clientApplication": "Software", "term": term})
        raw = self.fetcher.fetch(f"{APPLE_HINTS_URL}?{qs}", headers={"X-Apple-Store-Front": self.storefront})
        return parse_apple_hints(raw)

    def top_apps(self, term, limit=10):
        qs = urllib.parse.urlencode(
            {"term": term, "country": self.country, "entity": "software", "limit": limit}
        )
        return parse_itunes_search(self.fetcher.fetch(f"{ITUNES_SEARCH_URL}?{qs}"))


class GooglePlay:
    name = "play"
    supports_difficulty = False

    def __init__(self, fetcher, country="us", lang="en"):
        self.fetcher = fetcher
        self.country = country
        self.lang = lang

    def suggest(self, term):
        inner = json.dumps([[None, [term], [10], [2], 4]])
        freq = json.dumps([[["IJ4APc", inner, None, "generic"]]])
        qs = urllib.parse.urlencode({"rpcids": "IJ4APc", "hl": self.lang, "gl": self.country})
        raw = self.fetcher.fetch(
            f"{PLAY_SUGGEST_URL}?{qs}",
            data=urllib.parse.urlencode({"f.req": freq}),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        return parse_play_suggest(raw)

    def top_apps(self, term, limit=10):
        return None


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def popularity_score(term, min_prefix, position, list_size=10):
    """0-100. Terms that surface after fewer typed characters and higher in the
    list are searched more. Suggested-but-never-from-own-prefix terms get 5."""
    if min_prefix is None:
        return 5
    typed_fraction = min(1.0, min_prefix / max(len(term), 1))
    prefix_part = 1.0 - typed_fraction + 1.0 / max(len(term), 1)  # 1 char typed -> 1.0
    position_part = 1.0 - min(position, list_size - 1) / list_size
    return round(10 + 75 * min(prefix_part, 1.0) + 15 * position_part)


def difficulty_score(term, apps):
    """0-100 from the top apps for a term: how established they are (median
    rating count, log scale, 1M ratings = max) and how many use the term's
    words in their title."""
    if not apps:
        return 0
    counts = sorted(a["ratings"] for a in apps)
    median = counts[len(counts) // 2]
    strength = min(1.0, math.log10(median + 1) / 6)
    words = term.split()
    in_title = sum(all(w in a["title"].lower() for w in words) for a in apps) / len(apps)
    return round(100 * (0.65 * strength + 0.35 * in_title))


# --------------------------------------------------------------------------- #
# Research
# --------------------------------------------------------------------------- #
@dataclass
class Keyword:
    term: str
    min_prefix: int = None   # shortest typed prefix that surfaced the term
    position: int = None     # its rank in that suggestion list (0 = top)
    popularity: int = 0
    difficulty: int = None
    opportunity: int = None
    top_apps: list = field(default_factory=list)

    def observe(self, query, pos):
        if not self.term.startswith(query):
            return
        n = len(query)
        if self.min_prefix is None or n < self.min_prefix or (n == self.min_prefix and pos < self.position):
            self.min_prefix, self.position = n, pos

    def as_dict(self):
        return {
            "keyword": self.term,
            "popularity": self.popularity,
            "difficulty": self.difficulty,
            "opportunity": self.opportunity,
            "chars_typed": self.min_prefix,
            "position": None if self.position is None else self.position + 1,
            "top_apps": self.top_apps[:5],
        }


class Researcher:
    def __init__(self, store, probe=True, difficulty=True, limit=50, log=None):
        self.store = store
        self.probe = probe
        self.difficulty = difficulty and store.supports_difficulty
        self.limit = limit
        self.log = log or (lambda msg: None)
        self._cache = {}
        self.keywords = {}

    def suggest(self, query):
        query = normalize(query)
        if query not in self._cache:
            try:
                self._cache[query] = [normalize(t) for t in self.store.suggest(query)]
            except Exception as exc:
                self.log(f"  ! suggest '{query}' failed: {exc}")
                self._cache[query] = []
        return self._cache[query]

    def _run_query(self, query, add_new=True):
        for pos, term in enumerate(self.suggest(query)):
            kw = self.keywords.get(term)
            if kw is None:
                if not add_new or len(self.keywords) >= self.limit:
                    continue
                kw = self.keywords[term] = Keyword(term)
            kw.observe(normalize(query), pos)

    def _probe(self, kw):
        """Type the term one character at a time until it shows up."""
        for i in range(1, len(kw.term) + 1):
            prefix = kw.term[:i]
            if prefix.endswith(" "):
                continue
            if kw.min_prefix is not None and i >= kw.min_prefix:
                return
            self._run_query(prefix, add_new=False)
            if kw.min_prefix is not None and kw.min_prefix <= i:
                return

    def research(self, seed):
        seed = normalize(seed)
        if not seed:
            raise ValueError("seed keyword is empty")
        self.log(f"Expanding '{seed}' on {self.store.name}...")
        try:  # fail fast if the store is unreachable
            self._cache[seed] = [normalize(t) for t in self.store.suggest(seed)]
        except Exception as exc:
            raise RuntimeError(f"could not reach {self.store.name} autocomplete: {exc}") from exc
        self._run_query(seed)
        for letter in string.ascii_lowercase:
            self._run_query(f"{seed} {letter}")
        if seed not in self.keywords:
            self.keywords[seed] = Keyword(seed)
        self.log(f"  {len(self.keywords)} candidate keywords")

        if self.probe:
            self.log("Probing prefixes for popularity...")
            for kw in sorted(self.keywords.values(), key=lambda k: len(k.term)):
                self._probe(kw)

        for kw in self.keywords.values():
            kw.popularity = popularity_score(kw.term, kw.min_prefix, kw.position or 0)

        if self.difficulty:
            self.log("Scoring difficulty from top-ranking apps...")
            for kw in self.keywords.values():
                try:
                    apps = self.store.top_apps(kw.term) or []
                except Exception as exc:
                    self.log(f"  ! top apps '{kw.term}' failed: {exc}")
                    continue
                kw.top_apps = apps
                kw.difficulty = difficulty_score(kw.term, apps)
                kw.opportunity = round(kw.popularity * (1 - kw.difficulty / 100))

        return sorted(
            self.keywords.values(),
            key=lambda k: (k.opportunity if k.opportunity is not None else k.popularity, k.popularity),
            reverse=True,
        )


def make_store(store, country, lang, delay):
    fetcher = Fetcher(delay=delay)
    if store == "appstore":
        return AppStore(fetcher, country)
    if store == "play":
        return GooglePlay(fetcher, country, lang)
    raise ValueError(f"unknown store '{store}'")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def print_table(rows, out=sys.stdout):
    def fmt(v):
        return "-" if v is None else str(v)

    headers = ["keyword", "popularity", "difficulty", "opportunity", "chars_typed", "position"]
    data = [[fmt(r[h]) for h in headers] for r in rows]
    widths = [max(len(h), *(len(d[i]) for d in data)) if data else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line, file=out)
    print("-" * len(line), file=out)
    for d in data:
        print("  ".join(v.ljust(w) for v, w in zip(d, widths)), file=out)


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["keyword", "popularity", "difficulty", "opportunity", "chars_typed", "position", "top_apps"])
        for r in rows:
            w.writerow([
                r["keyword"], r["popularity"], r["difficulty"], r["opportunity"],
                r["chars_typed"], r["position"], " | ".join(a["title"] for a in r["top_apps"]),
            ])


def cmd_research(args):
    store = make_store(args.store, args.country, args.lang, args.delay)
    log = (lambda m: print(m, file=sys.stderr)) if not args.quiet else None
    researcher = Researcher(store, probe=not args.no_probe, difficulty=not args.no_difficulty,
                            limit=args.limit, log=log)
    rows = [k.as_dict() for k in researcher.research(args.seed)]
    if log:
        log(f"Done: {len(rows)} keywords, {store.fetcher.requests} requests.\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
    if args.csv:
        write_csv(rows, args.csv)
    print_table(rows)


def cmd_serve(args):
    from server import serve
    serve(args.host, args.port)


def main(argv=None):
    p = argparse.ArgumentParser(description="Find and score App Store / Google Play keywords.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("research", help="expand a seed keyword and score suggestions")
    r.add_argument("seed")
    r.add_argument("--store", choices=["appstore", "play"], default="appstore")
    r.add_argument("--country", default="us", help="two-letter country code (default us)")
    r.add_argument("--lang", default="en", help="Google Play language (default en)")
    r.add_argument("--limit", type=int, default=50, help="max keywords to collect (default 50)")
    r.add_argument("--delay", type=float, default=0.3, help="seconds between requests (default 0.3)")
    r.add_argument("--no-probe", action="store_true", help="skip prefix probing (faster, rougher popularity)")
    r.add_argument("--no-difficulty", action="store_true", help="skip difficulty scoring")
    r.add_argument("--csv", help="write results to CSV")
    r.add_argument("--json", help="write results to JSON")
    r.add_argument("-q", "--quiet", action="store_true")
    r.set_defaults(func=cmd_research)

    s = sub.add_parser("serve", help="run the local web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.country = getattr(args, "country", "us").lower()
    try:
        args.func(args)
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
