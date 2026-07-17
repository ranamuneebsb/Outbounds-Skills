#!/usr/bin/env python3
"""
gmaps_email_extractor.py — decoupled email enrichment for the Google Maps scraper.

Runs COMPLETELY SEPARATELY from the maps scraping. Fast mode (gosom scraper)
produces a CSV with a `website` column at ~275/min; this script reads that
column, concurrently fetches each site (homepage + a few likely contact pages),
extracts emails, filters out the usual regex noise, and writes an `emails`
column back. Because it's its own process, the maps scraping is never slowed.

Usage:
    python gmaps_email_extractor.py input.csv                 # -> input.emails.csv
    python gmaps_email_extractor.py in.csv -o out.csv -c 50
    python gmaps_email_extractor.py in.csv --no-contact-pages # homepage only (faster)

Notes:
  - Idempotent-ish: rows that already have a non-empty `emails` value are skipped,
    so you can re-run to fill only the gaps.
  - Pure stdlib + httpx + beautifulsoup4. No API keys, no cost.
"""
import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

csv.field_size_limit(10**9)  # long homepage-derived cells (learned from ICP runs)

# --- email pattern + noise filtering ---------------------------------------
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# file extensions that regex mistakes for a TLD (e.g. logo@2x.png, hero@3x.jpg)
BAD_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
           ".css", ".js", ".mp4", ".pdf", ".woff", ".ttf")
# placeholder / vendor / theme addresses that aren't the business's real inbox
NOISE_DOMAINS = (
    "example.com", "example.org", "domain.com", "email.com", "yourdomain.com",
    "mysite.com", "sentry.io", "sentry-next.wixpress.com", "wixpress.com",
    "wix.com", "godaddy.com", "squarespace.com", "shopify.com", "cloudflare.com",
    "schema.org", "w3.org", "googleapis.com", "gstatic.com", "jquery.com",
    "marketmymarket.com", "typemade.mx", "lab6.com", "sentry.wixpress.com",
)
NOISE_LOCALPARTS = ("filler", "your-email", "youremail", "email", "name",
                    "user", "username", "someone", "example", "test",
                    "no-reply", "noreply", "sentry")

CONTACT_PATHS = ("/contact", "/contact-us", "/contactus", "/about",
                 "/about-us", "/get-in-touch")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}


def clean_emails(candidates):
    """Dedupe + drop image-filename / placeholder / vendor noise."""
    out, seen = [], set()
    for raw in candidates:
        e = raw.strip().strip(".,;:").lower()
        if not e or e in seen:
            continue
        if e.endswith(BAD_EXT):
            continue
        domain = e.split("@", 1)[1] if "@" in e else ""
        local = e.split("@", 1)[0]
        if any(domain == d or domain.endswith("." + d) for d in NOISE_DOMAINS):
            continue
        if local in NOISE_LOCALPARTS:
            continue
        if len(e) > 100:  # runaway match
            continue
        seen.add(e)
        out.append(e)
    return out


def extract_from_html(html):
    """mailto: links first (clean), then a regex sweep of the raw HTML."""
    emails = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select('a[href^="mailto:"]'):
            addr = a.get("href", "")[len("mailto:"):].split("?")[0]
            if addr:
                emails.append(addr)
    except Exception:
        pass
    emails.extend(EMAIL_RE.findall(html))
    return clean_emails(emails)


def normalize_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


async def fetch(client, url, sem):
    async with sem:
        try:
            r = await client.get(url, headers=HEADERS, follow_redirects=True)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", "text/html"):
                return r.text
        except Exception:
            return None
    return None


async def emails_for_site(client, website, sem, try_contact):
    base = normalize_url(website)
    if not base:
        return []
    html = await fetch(client, base, sem)
    emails = extract_from_html(html) if html else []
    if emails or not try_contact:
        return emails
    # no email on homepage — try a few likely contact pages
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    for path in CONTACT_PATHS:
        html = await fetch(client, urljoin(root, path), sem)
        if html:
            emails = extract_from_html(html)
            if emails:
                return emails
    return []


async def run(rows, web_col, concurrency, timeout, try_contact):
    sem = asyncio.Semaphore(concurrency)
    done = {"n": 0, "hit": 0}
    total = sum(1 for r in rows if r.get(web_col, "").strip() and not r.get("emails", "").strip())
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=timeout, limits=limits, verify=False) as client:
        async def work(row):
            if row.get("emails", "").strip():      # already filled → skip
                return
            if not row.get(web_col, "").strip():
                return
            emails = await emails_for_site(client, row[web_col], sem, try_contact)
            row["emails"] = ", ".join(emails)
            done["n"] += 1
            if emails:
                done["hit"] += 1
            if done["n"] % 20 == 0 or done["n"] == total:
                pct = 100 * done["hit"] // max(done["n"], 1)
                print(f"  {done['n']}/{total} sites  |  {done['hit']} with email ({pct}%)",
                      file=sys.stderr)
        await asyncio.gather(*(work(r) for r in rows))
    return done


def main():
    ap = argparse.ArgumentParser(description="Concurrent email extractor for gmaps fast-mode CSVs")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("-c", "--concurrency", type=int, default=40)
    ap.add_argument("-t", "--timeout", type=float, default=12.0)
    ap.add_argument("--website-col", default="website")
    ap.add_argument("--no-contact-pages", action="store_true",
                    help="homepage only (faster, lower hit rate)")
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.output) if args.output else inp.with_suffix(".emails.csv")

    with open(inp, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])

    if args.website_col not in fields:
        sys.exit(f"ERROR: column '{args.website_col}' not found. Columns: {fields}")
    if "emails" not in fields:
        fields.append("emails")
    for r in rows:
        r.setdefault("emails", "")

    print(f"Loaded {len(rows)} rows from {inp.name}. Extracting emails "
          f"(concurrency={args.concurrency}, contact-pages={not args.no_contact_pages})...",
          file=sys.stderr)

    done = asyncio.run(run(rows, args.website_col, args.concurrency,
                           args.timeout, not args.no_contact_pages))

    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    pct = 100 * done["hit"] // max(done["n"], 1)
    print(f"\nDONE. {done['hit']}/{done['n']} sites yielded an email ({pct}%). "
          f"Wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
