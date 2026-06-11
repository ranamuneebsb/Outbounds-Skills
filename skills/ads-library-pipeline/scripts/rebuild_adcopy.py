#!/usr/bin/env python3
"""rebuild_adcopy.py — backfill the richest distinct ad copy per advertiser.

Single pass over the per-ad source (ads_slim.csv / merged_all.csv): for each
advertiser still in advertisers_clean.csv, collect the fullest distinct body
texts (main body, every card body, link_description) + distinct titles, then
write ad_copy_full (5 fullest, deduped) and ad_titles (3 distinct) back onto
advertisers_clean.csv. The classifier reads ad_copy_full, so richer copy here
= more rows classifiable from copy alone (before paying for domains/scraping).

    python rebuild_adcopy.py --workdir "D:/campaigns/acme"
"""
import csv, os, re, argparse
from urllib.parse import urlparse, parse_qs

csv.field_size_limit(10**9)

C_PAGE_URL  = ["ad/snapshot/page_profile_uri", "snapshot/pageProfileUri", "page_profile_uri", "page_url"]
C_PAGE_ID   = ["ad/page_id", "pageId", "pageID", "page_id", "ad/snapshot/page_id", "snapshot/pageId"]
C_PAGE_NAME = ["ad/page_name", "pageName", "page_name", "ad/snapshot/page_name", "snapshot/pageName"]

TOKEN = re.compile(r"\{\{.*?\}\}")
URL = re.compile(r"https?://\S+|www\.\S+", re.I)
EMO = ("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
       "\U00002190-\U000021FF\U00002300-\U000023FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0000200D]")
EMO_RUN = re.compile(EMO + r"(?:\s*" + EMO + r")+")
WS = re.compile(r"\s+")


def clean(t):
    if not t:
        return ""
    t = TOKEN.sub(" ", t)
    t = URL.sub(" ", t)
    t = t.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    t = EMO_RUN.sub(" ", t)
    return WS.sub(" ", t).strip()


def sig(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())[:150]


def normalize_url(raw):
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


def make_key(page_url_raw, page_id, page_name):
    k = normalize_url(page_url_raw)
    if k:
        return k
    if page_id:
        return "pid:" + page_id
    if page_name:
        return "name:" + page_name.lower().strip()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--input", default="", help="Override per-ad source (default ads_slim.csv | merged_all.csv)")
    args = ap.parse_args()

    DIR = args.workdir
    if args.input:
        MERGED = args.input if os.path.isabs(args.input) else os.path.join(DIR, args.input)
    else:
        slim = os.path.join(DIR, "ads_slim.csv")
        MERGED = slim if os.path.exists(slim) else os.path.join(DIR, "merged_all.csv")
    CLEAN = os.path.join(DIR, "advertisers_clean.csv")
    TMP = os.path.join(DIR, "advertisers_clean.tmp.csv")
    print(f"Per-ad source: {MERGED}")

    with open(MERGED, "r", encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    idx = {c: i for i, c in enumerate(header)}

    def idxs(cols):
        return [idx[c] for c in cols if c in idx]

    I_URL, I_PID, I_NAME = idxs(C_PAGE_URL), idxs(C_PAGE_ID), idxs(C_PAGE_NAME)
    I_BODY = idxs(["ad/snapshot/body/text", "snapshot/body/text", "body/text", "text"])
    I_CARD = [i for c, i in idx.items() if re.match(r"^(ad/)?snapshot/cards/\d+/body$", c)]
    I_LINKDESC = idxs(["ad/snapshot/link_description", "snapshot/linkDescription", "link_description"])
    I_TITLE = idxs(["ad/snapshot/title", "snapshot/title", "title"])

    def first(row, ii):
        for i in ii:
            if i < len(row):
                v = row[i]
                if v and v.strip():
                    return v.strip()
        return ""

    clean_rows = list(csv.DictReader(open(CLEAN, encoding="utf-8", newline="")))
    need = {}
    for r in clean_rows:
        k = make_key(r.get("page_url", ""), r.get("page_id", ""), r.get("page_name", ""))
        if k:
            need.setdefault(k, []).append(r)

    agg = {k: {"texts": [], "sigs": set(), "titles": [], "tsigs": set()} for k in need}
    with open(MERGED, "r", encoding="utf-8", newline="") as fh:
        rd = csv.reader(fh); next(rd)
        for row in rd:
            k = make_key(first(row, I_URL), first(row, I_PID), first(row, I_NAME))
            if k not in agg:
                continue
            a = agg[k]
            cands = []
            b = first(row, I_BODY)
            if b:
                cands.append(b)
            for i in I_CARD:
                if i < len(row) and row[i] and row[i].strip():
                    cands.append(row[i].strip())
            ld = first(row, I_LINKDESC)
            if ld:
                cands.append(ld)
            for c in cands:
                cc = clean(c)
                if len(cc) < 15:
                    continue
                s = sig(cc)
                if not s or s in a["sigs"]:
                    continue
                a["sigs"].add(s); a["texts"].append(cc)
            tt = clean(first(row, I_TITLE))
            if tt:
                ts = sig(tt)
                if ts and ts not in a["tsigs"]:
                    a["tsigs"].add(ts); a["titles"].append(tt)

    def build(a):
        texts = sorted(a["texts"], key=len, reverse=True)[:5]
        return " ||| ".join(t[:600] for t in texts), " | ".join(a["titles"][:3])

    for k, rs in need.items():
        full, titles = build(agg[k])
        for r in rs:
            r["ad_copy_full"] = full
            r["ad_titles"] = titles

    base = ["page_name", "page_id", "page_url", "active_ad_count", "ad_copy_sample",
            "ad_copy_full", "ad_titles", "title", "cta_text", "link_url", "page_likes",
            "page_category", "keywords_matched", "ad_library_link", "domain", "domain_type", "domain_source"]
    with open(TMP, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=base, extrasaction="ignore")
        w.writeheader()
        for r in clean_rows:
            r.setdefault("ad_copy_full", ""); r.setdefault("ad_titles", "")
            w.writerow(r)
    os.replace(TMP, CLEAN)

    N = len(clean_rows) or 1
    filled = sum(1 for r in clean_rows if r.get("ad_copy_full"))
    avg_old = sum(len(r.get("ad_copy_sample", "")) for r in clean_rows) / N
    avg_new = sum(len(r.get("ad_copy_full", "")) for r in clean_rows) / N
    print(f"Advertisers: {len(clean_rows)} | with ad_copy_full: {filled}")
    print(f"avg len  ad_copy_sample={avg_old:.0f}  ad_copy_full={avg_new:.0f}")
    print(f"Wrote {CLEAN}")


if __name__ == "__main__":
    main()
