#!/usr/bin/env python3
"""snap_and_export.py — niche normalization + final outreach-ready export.

The classifier occasionally returns an off-list niche string (close to, but not
exactly, one of the ICP's niche labels). Optionally snap those to canonical
labels via a user-supplied alias map, then write the final deliverable.

Alias map (optional): a JSON file of {"off-list niche": "canonical niche", ...}
passed with --niche-aliases. Campaign-specific, so it is NEVER baked in — if you
don't pass one, no snapping happens.

    python snap_and_export.py --workdir D:/c/acme [--niche-aliases D:/c/acme/aliases.json]

Reads qualified_advertisers.csv (already carries personalization if personalize
ran), writes qualified_advertisers_final.csv.
"""
import csv, os, json, argparse

csv.field_size_limit(10**9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--niche-aliases", default="", help="Optional JSON {off-list: canonical} map")
    args = ap.parse_args()

    DIR = args.workdir
    SRC = os.path.join(DIR, "qualified_advertisers.csv")
    OUT = os.path.join(DIR, "qualified_advertisers_final.csv")

    SNAP = {}
    if args.niche_aliases and os.path.exists(args.niche_aliases):
        SNAP = json.load(open(args.niche_aliases, encoding="utf-8"))

    rows = list(csv.DictReader(open(SRC, encoding="utf-8", newline="")))
    FIELDS = list(rows[0].keys())
    snapped = 0
    for r in rows:
        if r.get("niche") in SNAP:
            r["niche"] = SNAP[r["niche"]]
            snapped += 1

    def ad_count(r):
        try:
            return int(r.get("active_ad_count") or 0)
        except Exception:
            return 0

    rows.sort(key=ad_count, reverse=True)
    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

    N = len(rows) or 1
    with_dom = sum(1 for r in rows if (r.get("domain") or "").strip())
    ge3 = sum(1 for r in rows if ad_count(r) >= 3)
    print(f"Snapped {snapped} off-list niche rows" + ("" if SNAP else " (no alias map given)"))
    print(f"\nFinal qualified export: {len(rows)}  -> {OUT}")
    print(f"  WITH domain:    {with_dom}  ({with_dom/N*100:.1f}%)")
    print(f"  3+ active ads:  {ge3}  ({ge3/N*100:.1f}%)")
    print(f"\nTop 10 by active_ad_count:")
    for r in rows[:10]:
        print(f"  {ad_count(r):>5}  {str(r.get('page_name',''))[:38]:<38} | {str(r.get('niche',''))[:26]:<26} | {r.get('domain') or '(no domain)'}")


if __name__ == "__main__":
    main()
