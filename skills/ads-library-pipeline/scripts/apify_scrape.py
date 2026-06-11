#!/usr/bin/env python3
"""apify_scrape.py — scrape the Meta Ad Library via Apify, JSON not CSV.

Reads a USER-PROVIDED keyword file (one parent keyword per line) and a list of
countries, fires ALL keyword runs CONCURRENTLY through the apify-client API, then
pulls each run's results as a JSON dataset (NOT the CSV export — that's what
flattens into 4,000+ columns). During ingestion it keeps ONLY the fields the
pipeline needs and writes ads_slim.csv directly.

ads_slim.csv uses Apify's flat slash-path column names (ad/snapshot/body/text,
ad/page_id, ...) so dedupe_advertisers.py and rebuild_adcopy.py consume it
unchanged. _source_file carries the keyword.

    python apify_scrape.py --workdir D:/c/acme --keywords D:/c/acme/keywords.txt \
        --countries US,CA --max-ads 500

API token: ~/.olympolabs/apify.json only ({"api_key": "apify_api_..."}). The
actor and its input may need tweaking per actor — see --actor / --actor-input.
If JSON ingestion ever fails for a manual CSV drop, merge_csvs.py is the legacy
fallback that produces merged_all.csv instead.
"""
import csv, os, json, time, argparse, subprocess, sys
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

csv.field_size_limit(10**9)

# Default actor (a Meta Ad Library scraper that accepts a `urls` list of ad-library
# search URLs). Override with --actor; override its input with --actor-input.
APIFY_ACTOR = "curious_coder/facebook-ads-library-scraper"
MAX_CARDS = 10


def load_token():
    p = os.path.expanduser("~/.olympolabs/apify.json")
    with open(p) as f:
        d = json.load(f)
    return d.get("api_key") or d.get("token")


def ensure_client():
    try:
        from apify_client import ApifyClient  # noqa
    except ImportError:
        print("Installing apify-client ...", flush=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "apify-client"])
    from apify_client import ApifyClient
    return ApifyClient


def read_keywords(path):
    out = []
    for line in open(path, encoding="utf-8-sig"):
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def ad_library_url(keyword, country):
    return ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            f"&country={country}&q={quote(keyword)}&search_type=keyword_unordered&media_type=all")


def build_input(keyword, countries, max_ads, template):
    urls = [{"url": ad_library_url(keyword, c)} for c in countries]
    inp = dict(template) if template else {}
    inp.setdefault("urls", urls)
    inp.setdefault("count", max_ads)
    inp.setdefault("activeStatus", "active")
    return inp


# ---- slim field extraction (handles {ad:{...}} and flat {snapshot:{...}}) ----
def _ad(item):
    return item["ad"] if isinstance(item.get("ad"), dict) else item


def _txt(v):
    if isinstance(v, dict):
        return v.get("text", "")
    return v if v is not None else ""


def slim_row(item, keyword):
    ad = _ad(item)
    snap = ad.get("snapshot") or item.get("snapshot") or {}
    cats = snap.get("page_categories") or snap.get("pageCategories") or []
    if isinstance(cats, dict):
        cats = list(cats.values())
    row = {
        "_source_file": keyword,
        "ad/page_id": ad.get("page_id") or ad.get("pageId") or "",
        "ad/page_name": ad.get("page_name") or ad.get("pageName") or "",
        "ad/is_active": ad.get("is_active") if ad.get("is_active") is not None else item.get("isActive", ""),
        "ad/snapshot/page_profile_uri": snap.get("page_profile_uri") or snap.get("pageProfileUri") or "",
        "ad/snapshot/body/text": _txt(snap.get("body")),
        "ad/snapshot/title": snap.get("title") or "",
        "ad/snapshot/cta_text": snap.get("cta_text") or snap.get("ctaText") or "",
        "ad/snapshot/link_url": snap.get("link_url") or snap.get("linkUrl") or "",
        "ad/snapshot/link_description": snap.get("link_description") or snap.get("linkDescription") or "",
        "ad/snapshot/page_like_count": snap.get("page_like_count") or snap.get("pageLikeCount") or "",
        "ad/snapshot/page_categories/0": (cats[0] if cats else ""),
    }
    cards = snap.get("cards") or []
    for i in range(MAX_CARDS):
        c = cards[i] if i < len(cards) and isinstance(cards[i], dict) else {}
        row[f"ad/snapshot/cards/{i}/body"] = _txt(c.get("body")) if c else ""
        row[f"ad/snapshot/cards/{i}/link_url"] = (c.get("link_url") or c.get("linkUrl") or "") if c else ""
        row[f"ad/snapshot/cards/{i}/link_description"] = (c.get("link_description") or c.get("linkDescription") or "") if c else ""
        row[f"ad/snapshot/cards/{i}/title"] = (c.get("title") or "") if c else ""
    return row


def FIELDS():
    f = ["_source_file", "ad/page_id", "ad/page_name", "ad/is_active",
         "ad/snapshot/page_profile_uri", "ad/snapshot/body/text", "ad/snapshot/title",
         "ad/snapshot/cta_text", "ad/snapshot/link_url", "ad/snapshot/link_description",
         "ad/snapshot/page_like_count", "ad/snapshot/page_categories/0"]
    for i in range(MAX_CARDS):
        f += [f"ad/snapshot/cards/{i}/body", f"ad/snapshot/cards/{i}/link_url",
              f"ad/snapshot/cards/{i}/link_description", f"ad/snapshot/cards/{i}/title"]
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--keywords", required=True, help="USER-PROVIDED keyword file (one parent keyword per line)")
    ap.add_argument("--countries", default="US", help="Comma-separated ISO country codes, e.g. US,CA,GB")
    ap.add_argument("--max-ads", type=int, default=500, help="Max ads per keyword run (actor `count`)")
    ap.add_argument("--actor", default=APIFY_ACTOR)
    ap.add_argument("--actor-input", default="", help="Optional JSON file: base input merged into every run")
    ap.add_argument("--resume", action="store_true", help="Skip if ads_slim.csv already exists")
    args = ap.parse_args()

    DIR = args.workdir
    OUT = os.path.join(DIR, "ads_slim.csv")
    if args.resume and os.path.exists(OUT):
        print(f"--resume: {OUT} exists, skipping scrape.")
        return

    ApifyClient = ensure_client()
    client = ApifyClient(load_token())
    keywords = read_keywords(args.keywords)
    countries = [c.strip() for c in args.countries.split(",") if c.strip()]
    template = json.load(open(args.actor_input)) if args.actor_input else None
    print(f"Keywords: {len(keywords)} | countries: {countries} | actor: {args.actor}", flush=True)

    # 1) start ALL keyword runs concurrently, then wait for each to finish
    def launch(kw):
        run = client.actor(args.actor).start(run_input=build_input(kw, countries, args.max_ads, template))
        done = client.run(run["id"]).wait_for_finish()
        return kw, done

    runs = []
    with ThreadPoolExecutor(max_workers=min(16, len(keywords) or 1)) as ex:
        for f in as_completed([ex.submit(launch, kw) for kw in keywords]):
            kw, done = f.result()
            status = (done or {}).get("status")
            ds = (done or {}).get("defaultDatasetId")
            print(f"  run done: {kw!r}  status={status}  dataset={ds}", flush=True)
            if ds and status == "SUCCEEDED":
                runs.append((kw, ds))

    # 2) pull each dataset as JSON, slim, stream to ads_slim.csv
    total = 0
    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS(), restval="", extrasaction="ignore")
        w.writeheader()
        for kw, ds in runs:
            n = 0
            for item in client.dataset(ds).iterate_items():
                w.writerow(slim_row(item, kw))
                n += 1; total += 1
            print(f"  + {n:>7} ads  <-  {kw!r}", flush=True)

    print(f"\nWrote {total} ad rows -> {OUT}", flush=True)
    print(f"Columns kept: {len(FIELDS())} (slim; JSON ingestion avoided the wide CSV flatten)", flush=True)


if __name__ == "__main__":
    main()
