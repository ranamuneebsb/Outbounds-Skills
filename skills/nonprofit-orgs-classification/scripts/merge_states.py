"""
Step 2 - Merge the downloaded state CSVs into one file, deduped by EIN.

- Verifies a consistent schema (warns on mismatch), adds a source_state column.
- Dedupes by EIN (first occurrence wins) and reports cross-state duplicates.

Example:
    python merge_states.py --raw-dir D:/Run/raw --output D:/Run/merged_all_states.csv
"""
import argparse, os, csv, sys, glob
from collections import Counter

csv.field_size_limit(10**9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--ein-col", default="EIN")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.raw_dir, "eo_*.csv")))
    if not files:
        print(f"No eo_*.csv in {args.raw_dir}. Run download_states.py first.")
        sys.exit(1)
    print(f"Merging {len(files)} files from {args.raw_dir}\n")

    canonical = None
    ein_idx = None
    seen = {}
    cross_dupes = []
    per_state = []
    mismatches = []
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    with open(args.output, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.writer(out_f)
        for path in files:
            state = os.path.splitext(os.path.basename(path))[0].replace("eo_", "").upper()
            with open(path, "r", newline="", encoding="utf-8", errors="replace") as in_f:
                reader = csv.reader(in_f)
                try:
                    header = next(reader)
                except StopIteration:
                    per_state.append((state, 0, 0, 0)); continue
                if canonical is None:
                    canonical = header
                    for i, c in enumerate(header):
                        if c.strip().upper() == args.ein_col.upper():
                            ein_idx = i; break
                    if ein_idx is None:
                        print(f"ERROR: no '{args.ein_col}' column in {header}"); sys.exit(1)
                    writer.writerow(canonical + ["source_state"])
                elif header != canonical:
                    mismatches.append(state)
                raw = written = dupes = 0
                for row in reader:
                    raw += 1
                    if ein_idx >= len(row):
                        continue
                    ein = row[ein_idx].strip()
                    if ein and ein in seen:
                        dupes += 1; cross_dupes.append((ein, seen[ein], state)); continue
                    if ein:
                        seen[ein] = state
                    writer.writerow(row + [state]); written += 1
                per_state.append((state, raw, written, dupes))
                print(f"  {state:<3} read={raw:>8} written={written:>8} dupes={dupes:>5}")

    tot_r = sum(p[1] for p in per_state); tot_w = sum(p[2] for p in per_state); tot_d = sum(p[3] for p in per_state)
    print("\n" + "=" * 52)
    print(f"Files merged   : {len(files)}")
    print(f"Rows read      : {tot_r:,}")
    print(f"Unique EINs    : {tot_w:,}")
    print(f"Cross-state dup: {tot_d:,}")
    print(f"Output         : {args.output}")
    if mismatches:
        print(f"WARNING header differed in: {', '.join(mismatches)}")
    if cross_dupes:
        print(f"\nSample cross-state dup EINs (up to 10 of {len(cross_dupes)}):")
        for ein, a, b in cross_dupes[:10]:
            print(f"  {ein}: kept {a}, dropped {b}")
    print("=" * 52)


if __name__ == "__main__":
    main()
