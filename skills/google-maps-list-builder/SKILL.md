SKILL NAME: Google Maps List Builder
VERSION: 1.0
AUTHOR: OlympoLabs

DESCRIPTION:
Build a deduped list of businesses from Google Maps for cold outbound, given a
search term (e.g. "dentists", "roofing companies") and a place (city, state, or
explicit ZIPs). Expands the place into its US ZIP codes and scrapes each one for
full coverage, then optionally enriches every row with emails scraped from the
businesses' own websites. Two engines — a FAST mode (quick, ~20 results/ZIP,
basic fields) and a DETAILED mode (full per-place data). Trigger whenever the
user wants to "scrape Google Maps", "build a list of [business type] in [place]",
"get businesses in [city/state]", "find leads from maps", "pull companies for
outbound", or "get emails for businesses in [place]". ALWAYS ask the user which
mode (fast or detailed) before running.

# Google Maps List Builder

Builds a business list from Google Maps for cold outbound. You give it a search
term and a place; it expands the place into ZIP codes, scrapes each ZIP for
coverage, dedupes, and (optionally) enriches with emails.

## ⚠️ ALWAYS DO THIS FIRST — ask the user which mode

Before running anything, ask:

> **Which mode do you want — fast or detailed?**
> - **fast** — quick, ~20 results per ZIP, basic fields (name, phone, website, rating, hours, address). Best for wide coverage fast.
> - **detailed** — full per-place data (everything above + more), slower. Best when you need depth.

Do **not** pick for them. Wait for the answer, then run with `--mode fast` or
`--mode detailed`. Also confirm whether they want **emails** (adds the decoupled
enrichment stage).

## Prerequisites

- **Docker Desktop running** (the scraper runs in a container). If the engine is
  off, start it first (Windows: `Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"`, wait ~20-30s).
- **Python deps** for the email stage: `pip install -r skills/google-maps-list-builder/requirements.txt`
- The scraper image is pulled automatically on first run
  (`gosom/google-maps-scraper:latest-rod`).

## How to run

Everything goes through `run.py`. It resolves the place → ZIP codes from
the bundled `data/us_zipcodes.csv`, scrapes, and merges/dedupes by `place_id`.

```bash
# FAST mode (one container per ZIP centroid, parallel worker pool)
python run.py --query "dentists" --place "Austin, TX" --mode fast -o austin_dentists.csv

# DETAILED mode (one container, one input line per ZIP)
python run.py --query "coffee shops" --city Seattle --state WA --mode detailed -o out.csv

# Add decoupled email enrichment (separate stage, does NOT slow the scrape)
python run.py --query "plumbers" --state NV --mode fast --email -o nv_plumbers.csv

# Explicit ZIPs
python run.py --query "gyms" --zips 33101,33102,33109 --mode fast
```

Key flags: `--concurrency` (fast: parallel containers; detailed: scraper `-c`),
`--zoom`/`--radius` (fast), `--depth` (detailed), `--limit N` (cap ZIPs for a test run).

## Scaling big places with subagents

`run.py` already parallelizes internally, but for a **whole large state** (often
1,000+ ZIPs) you get better throughput by fanning out across subagents:

1. Resolve the ZIP list for the place (the `data/us_zipcodes.csv` filtered by
   state/city — or run `run.py` once with `--limit 0`-style dry logic).
2. Split the ZIP list into N roughly equal batches.
3. Spawn **N subagents in parallel**, each running:
   `python run.py --query "<term>" --zips <batch> --mode <mode> -o batch_<i>.csv`
4. Merge the `batch_*.csv` outputs and dedupe by `place_id` (same key `run.py` uses).

Rule of thumb: ~100-200 ZIPs per subagent. Small places (a single city, <100 ZIPs)
don't need fan-out — one `run.py` call is enough.

## The two-stage design (why emails don't slow the scrape)

Maps scraping and email extraction are **separate processes on purpose**:

1. **Scrape** (`run.py`) → produces a CSV with a `website` column at full speed.
2. **Email** (`gmaps_email_extractor.py`) → a concurrent async fetcher that
   reads that CSV, hits each website (homepage + likely contact pages), extracts
   emails (mailto-first, then filtered regex), and writes an `emails` column.

Passing `--email` runs stage 2 automatically after stage 1. You can also run it
by hand on any results CSV, even while the next scrape is running:

```bash
python gmaps_email_extractor.py results.csv -c 50          # -> results.emails.csv
python gmaps_email_extractor.py results.csv --no-contact-pages   # faster, homepage only
```

It's idempotent — re-running only fills rows that don't already have an email.

## Output

A CSV with one row per unique business: `title, category, address, phone,
website, review_count, review_rating, latitude, longitude, place_id, ...` (and
`emails` when the email stage runs).

## Credits

- Scraping engine: [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) (MIT) — used as a Docker dependency.
- ZIP dataset: [GeoNames](https://www.geonames.org/) postal data (CC BY 4.0).

See `CREDITS.md`.
