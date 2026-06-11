#!/usr/bin/env python3
"""merge_csvs.py — LEGACY fallback for manual CSV drops.

The primary ingestion path is apify_scrape.py, which pulls Apify datasets as
JSON and writes ads_slim.csv directly (avoiding the 4,058-column CSV flattening).
Use this ONLY when you have hand-downloaded Apify CSV exports in --workdir and
want to merge them by COLUMN NAME (never by position) into merged_all.csv.

    python merge_csvs.py --workdir "D:/campaigns/acme"

Output: <workdir>/merged_all.csv  (every union column, _source_file = filename).
dedupe_advertisers.py reads ads_slim.csv if present, else merged_all.csv.
"""
import csv, os, glob, hashlib, argparse

csv.field_size_limit(10**9)  # FB ad fields can be huge

# Pipeline-owned filenames that must never be treated as raw input.
PIPELINE_OUTPUTS = {
    "merged_all.csv", "ads_slim.csv", "advertisers_deduped.csv",
    "advertisers_clean.csv", "advertisers_excluded.csv",
    "qualified_advertisers.csv", "qualified_advertisers_final.csv",
    "personalize_unmatched.csv",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True, help="Campaign folder holding the raw CSV drops")
    args = ap.parse_args()

    DIR = args.workdir
    OUT = os.path.join(DIR, "merged_all.csv")

    files = sorted(glob.glob(os.path.join(DIR, "*.csv")))
    files = [f for f in files if os.path.basename(f).lower() not in PIPELINE_OUTPUTS]

    # 1) Dedupe exact-duplicate files by content hash
    seen_hash, unique_files, skipped = {}, [], []
    for f in files:
        h = hashlib.md5(open(f, "rb").read()).hexdigest()
        if h in seen_hash:
            skipped.append((os.path.basename(f), os.path.basename(seen_hash[h])))
        else:
            seen_hash[h] = f
            unique_files.append(f)

    # 2) Pass 1: build union of all column names, preserving first-seen order
    master, master_set = [], set()
    for f in unique_files:
        with open(f, "r", encoding="utf-8-sig", newline="") as fh:
            try:
                header = next(csv.reader(fh))
            except StopIteration:
                header = []
        for c in header:
            if c not in master_set:
                master_set.add(c)
                master.append(c)

    # 3) Pass 2: stream rows, mapping each by column NAME into the master header
    final_header = ["_source_file"] + master
    total = 0
    with open(OUT, "w", encoding="utf-8", newline="") as out:
        w = csv.DictWriter(out, fieldnames=final_header, restval="", extrasaction="ignore")
        w.writeheader()
        for f in unique_files:
            name = os.path.basename(f)
            with open(f, "r", encoding="utf-8-sig", newline="") as fh:
                n = 0
                for row in csv.DictReader(fh):
                    row["_source_file"] = name
                    w.writerow(row)
                    n += 1
                    total += 1
            print(f"  + {n:>7} rows  <-  {name}")

    print(f"\nMerged {len(unique_files)} files -> {OUT}")
    print(f"Total data rows: {total}")
    print(f"Total columns:   {len(final_header)} (incl. _source_file)")
    if skipped:
        print(f"\nSkipped {len(skipped)} exact-duplicate file(s):")
        for s, orig in skipped:
            print(f"  - {s}  (identical to {orig})")


if __name__ == "__main__":
    main()
