#!/usr/bin/env python3
"""cleanup_pass.py — normalize domains and pre-filter obvious non-ICP rows.

  - Reclassify aggregator/tracker hosts to blank domain (trackers/aggregators
    are NOT domains: doubleclick, whatsapp, linktree, app stores, calendly...).
  - Normalize remaining real domains to their root (keeping multi-part ccTLDs).
  - Drop rows whose FB page_category is on the remove list (consumer-y verticals).
  - Drop content-farm domains (drama/reel/novel/short...).

    python cleanup_pass.py --workdir "D:/campaigns/acme"

Input : <workdir>/advertisers_deduped.csv
Output: <workdir>/advertisers_clean.csv  +  advertisers_excluded.csv
"""
import csv, os, re, argparse
from collections import Counter

csv.field_size_limit(10**9)

# ---- Aggregator / tracker hosts (reclassify -> aggregator, blank domain) ----
AGG = {
    "api.whatsapp.com", "whatsapp.com", "fb.com", "m.facebook.com", "play.google.com",
    "itunes.apple.com", "apps.apple.com", "yelp.com", "amazon.com", "youtube.com",
    "calendly.com", "eventbrite.com", "docs.google.com", "forms.gle", "zoom.us",
    "google.com", "linkedin.com", "tiktok.com", "zillow.com", "linktr.ee",
    "doubleclick.net", "googleadservices.com",
}


def is_aggregator(host):
    return any(host == a or host.endswith("." + a) for a in AGG)


# ---- Root-domain normalization (keep multi-part country TLDs) ----
MULTI_TLD = {
    "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz", "net.nz", "org.nz",
    "co.za", "org.za", "com.br", "net.br", "org.br", "gov.br", "co.in", "net.in",
    "org.in", "firm.in", "gen.in", "ind.in", "com.mx", "com.ar", "com.co", "com.pe",
    "com.ph", "com.sg", "com.my", "com.tr", "com.tw", "com.hk", "com.ua", "com.pk",
    "com.ng", "com.eg", "co.jp", "or.jp", "ne.jp", "co.kr", "co.id", "co.th", "co.il",
}


def root_domain(host):
    if not host:
        return host
    lead_www = host.startswith("www.")
    h = host[4:] if lead_www else host
    parts = h.split(".")
    if len(parts) <= 2:
        root = h
    else:
        last2 = ".".join(parts[-2:])
        root = ".".join(parts[-3:]) if (last2 in MULTI_TLD and len(parts) >= 3) else last2
    return ("www." + root) if lead_www else root


DEFAULT_REMOVE_CATS = {c.strip().lower() for c in [
    "real estate", "auto dealer", "auto service", "home care", "home repair",
    "appliance repair", "landscaping", "beauty salon", "health/beauty", "shopping",
    "medical service", "book", "media/news company", "topic", "digital creator",
    "public figure", "nonprofit organization", "education website",
    "college & university", "school", "health & wellness website", "sports team",
    "restaurant", "residence", "home & garden", "personal blog", "author",
    "musician/band", "entertainment website",
]}

FARM = re.compile(r"(drama|reel|novel|short)", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--remove-cats", default="", help="Optional file: one page_category per line to drop (replaces defaults)")
    args = ap.parse_args()

    DIR = args.workdir
    SRC = os.path.join(DIR, "advertisers_deduped.csv")
    CLEAN = os.path.join(DIR, "advertisers_clean.csv")
    EXCL = os.path.join(DIR, "advertisers_excluded.csv")

    remove_cats = DEFAULT_REMOVE_CATS
    if args.remove_cats and os.path.exists(args.remove_cats):
        remove_cats = {l.strip().lower() for l in open(args.remove_cats, encoding="utf-8") if l.strip()}

    rows = list(csv.DictReader(open(SRC, encoding="utf-8", newline="")))
    clean_rows, excl_rows, n_reclass = [], [], 0

    for r in rows:
        dom = (r.get("domain") or "").strip().lower()
        if dom and is_aggregator(dom):
            r["domain"], r["domain_type"], dom = "", "aggregator", ""
            n_reclass += 1
        elif dom:
            dom = root_domain(dom)
            r["domain"] = dom

        cat = (r.get("page_category") or "").strip().lower()
        if cat in remove_cats:
            r["exclude_reason"] = f"category:{cat}"
            excl_rows.append(r)
            continue
        if dom and FARM.search(dom):
            r["exclude_reason"] = "content_farm_domain"
            excl_rows.append(r)
            continue
        clean_rows.append(r)

    OUT_FIELDS = ["page_name", "page_id", "page_url", "active_ad_count", "ad_copy_sample",
                  "title", "cta_text", "link_url", "page_likes", "page_category",
                  "keywords_matched", "ad_library_link", "domain", "domain_type", "domain_source"]
    with open(CLEAN, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(clean_rows)
    with open(EXCL, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_FIELDS + ["exclude_reason"], extrasaction="ignore")
        w.writeheader(); w.writerows(excl_rows)

    N = len(clean_rows) or 1
    with_dom = sum(1 for r in clean_rows if (r.get("domain") or "").strip())
    print(f"Input advertisers:         {len(rows)}")
    print(f"Reclassified -> aggregator:{n_reclass}")
    print(f"Excluded (category+farm):  {len(excl_rows)}")
    print(f"FINAL clean advertisers:   {len(clean_rows)}")
    print(f"  with domain:    {with_dom}  ({with_dom/N*100:.1f}%)")
    print(f"  missing domain: {len(clean_rows)-with_dom}  ({(len(clean_rows)-with_dom)/N*100:.1f}%)")
    print(f"\nTop 20 remaining categories:")
    cc = Counter((r.get("page_category") or "(none)").strip() or "(none)" for r in clean_rows)
    for cat, c in cc.most_common(20):
        print(f"  {c:>6}  {cat}")
    print(f"\nWrote {CLEAN}\nWrote {EXCL}")


if __name__ == "__main__":
    main()
