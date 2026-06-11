#!/usr/bin/env python3
"""dedupe_advertisers.py — collapse per-ad rows to one row per ADVERTISER.

Dedupe key priority: normalized FB page URL  ->  page_id  ->  page_name.
This is the single biggest cost saver in the pipeline (typically a ~4-5x row
collapse) and it runs BEFORE anything paid. Always dedupe by FB page URL first.

Also does the FREE Level-1 domain extraction from the ad's own link_url
(clean / funnel / social-or-none) so cleanup_pass.py and the classifier can run
before a single paid domain lookup.

    python dedupe_advertisers.py --workdir "D:/campaigns/acme"

Input : <workdir>/ads_slim.csv  (preferred)  or  merged_all.csv  (legacy)
Output: <workdir>/advertisers_deduped.csv
"""
import csv, os, re, random, argparse
from urllib.parse import urlparse, parse_qs
from collections import Counter

csv.field_size_limit(10**9)
random.seed(42)

# Column variants (cross-format: snake / camelCase / Apify flat slash-path).
C_PAGE_URL  = ["ad/snapshot/page_profile_uri", "snapshot/pageProfileUri", "page_profile_uri", "page_url"]
C_PAGE_ID   = ["ad/page_id", "pageId", "pageID", "page_id", "ad/snapshot/page_id", "snapshot/pageId"]
C_PAGE_NAME = ["ad/page_name", "pageName", "page_name", "ad/snapshot/page_name", "snapshot/pageName"]
C_BODY      = ["ad/snapshot/body/text", "snapshot/body/text", "body/text", "text"]
C_TITLE     = ["ad/snapshot/title", "snapshot/title", "title"]
C_CTA       = ["ad/snapshot/cta_text", "snapshot/ctaText", "cta_text"]
C_LINK      = ["ad/snapshot/link_url", "link_url", "ad/snapshot/cards/0/link_url",
               "snapshot/linkUrl", "snapshot/cards/0/linkUrl"]
C_LIKES     = ["ad/snapshot/page_like_count", "snapshot/pageLikeCount", "page_like_count", "page_likes"]
C_CATEGORY  = ["ad/snapshot/page_categories/0", "snapshot/pageCategories/0", "page_categories/0", "page_category"]

# ---- free ad-link domain extraction (folds the old domain_extract.py) ----
SOCIAL = {"facebook.com", "instagram.com", "wa.me", "m.me", "fb.me", "fb.com",
          "messenger.com", "whatsapp.com", "api.whatsapp.com"}


def host_of(url):
    if not url:
        return ""
    u = url.strip()
    if "//" not in u:
        u = "http://" + u
    try:
        h = urlparse(u).netloc.lower()
    except Exception:
        return ""
    if h.startswith("www."):
        h = h[4:]
    return h.split(":")[0]


def is_social(h):
    return any(h == s or h.endswith("." + s) for s in SOCIAL)


def is_funnel(h):
    return ("clickfunnels" in h or "linktr.ee" in h or "linktree" in h
            or "bit.ly" in h or h.startswith("lp."))


def first_nonempty(row, cols):
    for c in cols:
        v = row.get(c)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


def normalize_url(raw):
    """Return a normalized FB page key, or None."""
    if not raw:
        return None
    raw = raw.strip().lower()
    if "//" not in raw:
        raw = "http://" + raw
    try:
        p = urlparse(raw)
    except Exception:
        return None
    path = p.path or ""
    if "profile.php" in path:
        ids = parse_qs(p.query).get("id")
        if ids and ids[0].strip():
            return "fbid:" + ids[0].strip()
    slug = path.strip("/").split("/")[0] if path else ""
    return ("fbslug:" + slug) if slug else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", default="", help="Override input CSV (default ads_slim.csv | merged_all.csv)")
    args = ap.parse_args()

    DIR = args.workdir
    if args.input:
        SRC = args.input if os.path.isabs(args.input) else os.path.join(DIR, args.input)
    else:
        slim = os.path.join(DIR, "ads_slim.csv")
        SRC = slim if os.path.exists(slim) else os.path.join(DIR, "merged_all.csv")
    OUT = os.path.join(DIR, "advertisers_deduped.csv")
    print(f"Input: {SRC}")

    groups, match_type, total_rows = {}, {}, 0
    with open(SRC, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            total_rows += 1
            page_url_raw = first_nonempty(row, C_PAGE_URL)
            page_id      = first_nonempty(row, C_PAGE_ID)
            page_name    = first_nonempty(row, C_PAGE_NAME)

            url_key = normalize_url(page_url_raw)
            if url_key:
                key, mtype = url_key, "page_url"
            elif page_id:
                key, mtype = "pid:" + page_id, "page_id"
            elif page_name:
                key, mtype = "name:" + page_name.lower().strip(), "page_name"
            else:
                continue

            g = groups.get(key)
            if g is None:
                g = {"page_name": "", "page_id": "", "page_url": "", "count": 0,
                     "bodies": [], "title": "", "cta_text": "", "link_first": "",
                     "link_nonfb": "", "page_likes": "", "page_category": "", "sources": set()}
                groups[key] = g
                match_type[key] = mtype

            g["count"] += 1
            if not g["page_name"] and page_name:   g["page_name"] = page_name
            if not g["page_id"] and page_id:        g["page_id"] = page_id
            if not g["page_url"] and page_url_raw:  g["page_url"] = page_url_raw
            if not g["title"]:         g["title"]         = first_nonempty(row, C_TITLE)
            if not g["cta_text"]:      g["cta_text"]      = first_nonempty(row, C_CTA)
            if not g["page_likes"]:    g["page_likes"]    = first_nonempty(row, C_LIKES)
            if not g["page_category"]: g["page_category"] = first_nonempty(row, C_CATEGORY)

            body = first_nonempty(row, C_BODY)
            if body and body not in g["bodies"] and len(g["bodies"]) < 3:
                g["bodies"].append(body)

            link = first_nonempty(row, C_LINK)
            if link:
                if not g["link_first"]:
                    g["link_first"] = link
                if not g["link_nonfb"] and "facebook.com" not in link.lower():
                    g["link_nonfb"] = link

            src = (row.get("_source_file") or row.get("keyword") or "").strip()
            if src:
                g["sources"].add(src)

    # Build output rows + free ad-link domain extraction
    out_rows = []
    dom_clean = dom_funnel = dom_none = 0
    for key, g in groups.items():
        link_url = g["link_nonfb"] or g["link_first"]
        sample = " | ".join(b[:200] for b in g["bodies"])
        pid = g["page_id"]
        ad_lib = ("https://www.facebook.com/ads/library/?view_all_page_id=" + pid) if pid else ""

        h = host_of(link_url)
        if not h or is_social(h):
            domain, dtype, dsrc = "", "", ""
            dom_none += 1
        elif is_funnel(h):
            domain, dtype, dsrc = h, "funnel", "ad_link"
            dom_funnel += 1
        else:
            domain, dtype, dsrc = h, "clean", "ad_link"
            dom_clean += 1

        out_rows.append({
            "page_name": g["page_name"], "page_id": pid, "page_url": g["page_url"],
            "active_ad_count": g["count"], "ad_copy_sample": sample, "title": g["title"],
            "cta_text": g["cta_text"], "link_url": link_url, "page_likes": g["page_likes"],
            "page_category": g["page_category"], "keywords_matched": ", ".join(sorted(g["sources"])),
            "ad_library_link": ad_lib, "domain": domain, "domain_type": dtype, "domain_source": dsrc,
        })

    FIELDS = ["page_name", "page_id", "page_url", "active_ad_count", "ad_copy_sample",
              "title", "cta_text", "link_url", "page_likes", "page_category",
              "keywords_matched", "ad_library_link", "domain", "domain_type", "domain_source"]
    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out_rows)

    # ---- report ----
    mt = Counter(match_type.values())
    N = len(out_rows) or 1
    print(f"Input ad rows:        {total_rows}")
    print(f"Unique advertisers:   {len(groups)}  ({total_rows/max(len(groups),1):.1f}x collapse)")
    print(f"\nMatched by:  page_url={mt.get('page_url',0)}  page_id={mt.get('page_id',0)}  page_name={mt.get('page_name',0)}")
    print(f"\nFree ad-link domains:  clean={dom_clean} ({dom_clean/N*100:.1f}%)  funnel={dom_funnel}  none={dom_none}")
    print(f"\nTop 15 advertisers by active_ad_count:")
    for i, a in enumerate(sorted(out_rows, key=lambda x: x["active_ad_count"], reverse=True)[:15], 1):
        nm = (a["page_name"] or "(no name)")[:45]
        print(f"  {i:>2}. {a['active_ad_count']:>6}  {nm:<45}  id={a['page_id']}")
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
