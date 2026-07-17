#!/usr/bin/env python3
"""
run.py — Google Maps list builder (ZIP-aware orchestrator).

Given a business search term and a place (city / state / explicit ZIPs), this
expands the place into its constituent US ZIP codes and scrapes each one, then
merges + dedupes into a single CSV. Two engines:

  fast     — quick, ~20 results per ZIP, basic fields (name/phone/website/etc).
             One container per ZIP centroid, run in a parallel worker pool.
  detailed — full per-place detail. One container, one input line per ZIP
             ("<query> in <zip>"), using the scraper's own concurrency.

The underlying scraper is the open-source gosom/google-maps-scraper (MIT),
pulled as a Docker image — this script only orchestrates it.

Examples:
  python run.py --query "dentists" --place "Austin, TX" --mode fast
  python run.py --query "coffee shops" --city Seattle --state WA --mode detailed --email
  python run.py --query "plumbers" --zips 33101,33102,33109 --mode fast -o out.csv
"""
import argparse
import concurrent.futures as cf
import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ZIP_CSV = HERE / "data" / "us_zipcodes.csv"
DEFAULT_IMAGE = "gosom/google-maps-scraper:latest-rod"

csv.field_size_limit(10**9)


# --------------------------------------------------------------------------- #
# ZIP resolution
# --------------------------------------------------------------------------- #
def load_zipcodes():
    if not ZIP_CSV.exists():
        sys.exit(f"ERROR: ZIP dataset not found at {ZIP_CSV}")
    with open(ZIP_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def resolve_zips(args, db):
    """Return a list of {zip, city, state_abbr, lat, lng} for the requested place."""
    if args.zips:
        wanted = {z.strip() for z in args.zips.split(",") if z.strip()}
        rows = [r for r in db if r["zip"] in wanted]
        missing = wanted - {r["zip"] for r in rows}
        if missing:
            print(f"WARN: {len(missing)} ZIP(s) not in dataset: {sorted(missing)[:10]}",
                  file=sys.stderr)
        return rows

    city, state = args.city, args.state
    if args.place:
        # "Austin, TX" or "Austin TX" -> city, state
        parts = [p.strip() for p in args.place.replace(",", " ").split() if p.strip()]
        if len(parts) >= 2 and len(parts[-1]) == 2:
            state = parts[-1].upper()
            city = " ".join(parts[:-1])
        else:
            city = args.place.strip()

    def norm(s):
        return (s or "").strip().lower()

    rows = db
    if state:
        st = state.upper()
        rows = [r for r in rows if r["state_abbr"].upper() == st or norm(r["state"]) == norm(state)]
    if city:
        rows = [r for r in rows if norm(r["city"]) == norm(city)]

    if not rows:
        sys.exit(f"ERROR: no ZIP codes matched place=({args.place or ''}) "
                 f"city=({city or ''}) state=({state or ''}). Check spelling.")
    return rows


# --------------------------------------------------------------------------- #
# Docker helpers
# --------------------------------------------------------------------------- #
def docker_available():
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
        return True
    except Exception:
        return False


def run_scraper(workdir, input_name, results_name, extra_args, image, timeout):
    """Invoke one scraper container. workdir is bind-mounted to /app."""
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{workdir}:/app",
        image,
        "-input", f"/app/{input_name}",
        "-results", f"/app/{results_name}",
    ] + extra_args
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"  WARN: container timed out ({results_name})", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Scrape modes
# --------------------------------------------------------------------------- #
def scrape_fast(zips, query, args, workdir):
    """One container per ZIP centroid, parallel worker pool."""
    tasks = []
    for i, z in enumerate(zips):
        qfile = f"q_{i}.txt"
        rfile = f"r_{i}.csv"
        (Path(workdir) / qfile).write_text(query + "\n", encoding="utf-8")
        extra = [
            "-fast-mode",
            "-geo", f"{z['lat']},{z['lng']}",
            "-zoom", str(args.zoom),
            "-radius", str(args.radius),
            "-c", "4",
            "-exit-on-inactivity", "40s",
        ]
        tasks.append((z, qfile, rfile, extra))

    done = {"n": 0}
    total = len(tasks)

    def work(t):
        z, qfile, rfile, extra = t
        run_scraper(workdir, qfile, rfile, extra, args.image, args.per_zip_timeout)
        done["n"] += 1
        if done["n"] % 5 == 0 or done["n"] == total:
            print(f"  scraped {done['n']}/{total} ZIPs", file=sys.stderr)
        return rfile

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        result_files = list(ex.map(work, tasks))
    return [str(Path(workdir) / rf) for rf in result_files]


def scrape_detailed(zips, query, args, workdir):
    """One container, one input line per ZIP; scraper handles concurrency."""
    lines = [f"{query} in {z['zip']}" for z in zips]
    (Path(workdir) / "queries.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    extra = [
        "-depth", str(args.depth),
        "-c", str(max(args.concurrency, 4)),
        "-exit-on-inactivity", "60s",
    ]
    if args.email_in_scraper:
        extra.append("-email")
    run_scraper(workdir, "queries.txt", "detailed.csv", extra, args.image, args.total_timeout)
    return [str(Path(workdir) / "detailed.csv")]


# --------------------------------------------------------------------------- #
# Merge + dedupe
# --------------------------------------------------------------------------- #
def merge_dedupe(result_files, out_path):
    header = None
    seen = set()
    kept = []
    for rf in result_files:
        p = Path(rf)
        if not p.exists() or p.stat().st_size == 0:
            continue
        with open(p, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if header is None:
                header = reader.fieldnames
            for row in reader:
                key = (row.get("place_id") or row.get("cid") or
                       row.get("link") or row.get("title"))
                if not key or key in seen:
                    continue
                seen.add(key)
                kept.append(row)
    if header is None:
        print("WARN: no results produced by any ZIP.", file=sys.stderr)
        header = ["title"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(kept)
    return len(kept)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Google Maps list builder (ZIP-aware)")
    ap.add_argument("--query", required=True, help="business search term, e.g. 'dentists'")
    loc = ap.add_argument_group("location (pick one)")
    loc.add_argument("--place", help='"Austin, TX"')
    loc.add_argument("--city")
    loc.add_argument("--state", help="2-letter abbr or full name")
    loc.add_argument("--zips", help="comma-separated explicit ZIPs")
    ap.add_argument("--mode", required=True, choices=["fast", "detailed"],
                    help="fast = quick/basic, detailed = full per-place")
    ap.add_argument("-o", "--out", default="gmaps_results.csv")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="fast: parallel containers; detailed: scraper -c")
    ap.add_argument("--zoom", type=int, default=13)
    ap.add_argument("--radius", type=int, default=8000)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--limit", type=int, help="cap number of ZIPs (testing)")
    ap.add_argument("--email", action="store_true",
                    help="run decoupled email extraction after scraping")
    ap.add_argument("--email-in-scraper", action="store_true",
                    help="(detailed only) fetch emails inside the scraper instead")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--per-zip-timeout", type=int, default=180)
    ap.add_argument("--total-timeout", type=int, default=3600)
    ap.add_argument("--keep-workdir", action="store_true")
    args = ap.parse_args()

    if not (args.place or args.city or args.state or args.zips):
        sys.exit("ERROR: specify a location: --place / --city / --state / --zips")

    if not docker_available():
        sys.exit("ERROR: Docker engine not reachable. Start Docker Desktop and retry.")

    db = load_zipcodes()
    zips = resolve_zips(args, db)
    if args.limit:
        zips = zips[:args.limit]

    label = args.place or f"{args.city or ''} {args.state or ''}".strip() or "explicit ZIPs"
    print(f"Place: {label}  ->  {len(zips)} ZIP codes", file=sys.stderr)
    print(f"Query: '{args.query}'   Mode: {args.mode}", file=sys.stderr)

    workdir = tempfile.mkdtemp(prefix="gmaps_")
    try:
        if args.mode == "fast":
            result_files = scrape_fast(zips, args.query, args, workdir)
        else:
            result_files = scrape_detailed(zips, args.query, args, workdir)

        n = merge_dedupe(result_files, args.out)
        print(f"\nMerged + deduped: {n} unique businesses -> {args.out}", file=sys.stderr)
    finally:
        if not args.keep_workdir:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)

    if args.email:
        extractor = HERE / "gmaps_email_extractor.py"  # same folder (flat layout)
        enriched = str(Path(args.out).with_suffix(".emails.csv"))
        print(f"\nRunning decoupled email extraction -> {enriched}", file=sys.stderr)
        subprocess.run([sys.executable, str(extractor), args.out,
                        "-o", enriched, "-c", "50"])
        print(f"Enriched file: {enriched}", file=sys.stderr)


if __name__ == "__main__":
    main()
