#!/usr/bin/env python3
"""finalize.py — backfill domains from web search and export the Qualified list.

  - Fill blank `domain` from `domain_found` (the root domain the rescue/search
    passes turned up), tagging domain_source=web_search.
  - Filter to icp_verdict==Qualified, sort by active_ad_count desc.
  - Write qualified_advertisers.csv + print coverage stats.

    python finalize.py --workdir D:/c/acme
"""
import csv, os, argparse
from collections import Counter

csv.field_size_limit(10**9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()

    DIR = args.workdir
    SRC = os.path.join(DIR, "advertisers_clean.csv")
    OUT = os.path.join(DIR, "qualified_advertisers.csv")

    rows = list(csv.DictReader(open(SRC, encoding="utf-8", newline="")))
    FIELDS = list(rows[0].keys())

    backfilled = 0
    for r in rows:
        if not (r.get("domain") or "").strip() and (r.get("domain_found") or "").strip():
            r["domain"] = r["domain_found"].strip()
            r["domain_source"] = "web_search"
            backfilled += 1
    with open(SRC, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

    def ad_count(r):
        try:
            return int(r.get("active_ad_count") or 0)
        except Exception:
            return 0

    qual = sorted([r for r in rows if r.get("icp_verdict") == "Qualified"], key=ad_count, reverse=True)
    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(qual)

    v = Counter(r.get("icp_verdict") for r in rows)
    N = len(qual) or 1
    qw = sum(1 for r in qual if (r.get("domain") or "").strip())
    ge3 = sum(1 for r in qual if ad_count(r) >= 3)
    print(f"Backfilled domain from web search: {backfilled} rows\n")
    print(f"=== WHOLE LIST ({len(rows)}) ===")
    for k in ["Qualified", "Disqualified", "Review", "error"]:
        print(f"  {k:<13} {v.get(k,0)}")
    print(f"\n=== QUALIFIED ({len(qual)}) -> {os.path.basename(OUT)} ===")
    print(f"  WITH domain:    {qw}  ({qw/N*100:.1f}%)")
    print(f"  WITHOUT domain: {len(qual)-qw}  ({(len(qual)-qw)/N*100:.1f}%)")
    print(f"  3+ active ads:  {ge3}  ({ge3/N*100:.1f}%)")


if __name__ == "__main__":
    main()
