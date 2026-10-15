# ASO Keyword Finder

Finds App Store / Google Play keywords from a seed word and scores them, using the
same public signals commercial ASO tools rely on. Python 3.9+, no dependencies.

## Run

```bash
# Command line
python3 keyfinder.py research "meditation" --store appstore --country us --csv out.csv
python3 keyfinder.py research "meditation" --store play --country gb

# Web UI -> http://127.0.0.1:8000
python3 keyfinder.py serve
```

Useful flags: `--limit 50` (max keywords), `--no-probe` (faster, rougher popularity),
`--no-difficulty`, `--delay 0.3` (seconds between requests), `--json out.json`.

## How it works

1. **Discover**: queries store autocomplete for the seed and `seed a` … `seed z`.
   - App Store: `search.itunes.apple.com/.../MZSearchHints.woa/wa/hints` with the storefront header
   - Google Play: the `IJ4APc` suggest RPC on `play.google.com/_/PlayStoreUi/data/batchexecute`
2. **Popularity (0–100)**: types each keyword one character at a time until it
   appears in the suggestions. Fewer characters typed and a higher position mean
   more searches. This is a relative proxy, not a search count.
3. **Difficulty (0–100, App Store only)**: top 10 apps from the iTunes Search API:
   median rating count (log scale, 1M ratings = max) weighted 65%, and the share
   of titles containing the keyword weighted 35%.
4. **Opportunity**: `popularity × (1 − difficulty/100)`. Results sort by this.

## Limitations

- These are unofficial endpoints. They can change or rate-limit you without notice.
  Keep the delay at 0.3s or more.
- Popularity is a heuristic. For real numbers on iOS, compare against Apple's Search
  Popularity score (Apple Ads), and use your own Apple Ads search-term report and
  Play Console search-term report as ground truth.
- Google Play difficulty isn't implemented: Play has no public search API.

## Tests

```bash
python3 -m unittest -v
```
