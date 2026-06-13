"""
Step 5 - Dedupe a working list against a previously-emailed EIN list.

Combines one or more input CSVs (same schema), drops rows whose EIN appears in the
emailed file, and writes fresh + overlap (audit) files. EINs are normalized on BOTH
sides before matching: strip, remove dashes, uppercase, kept as string (no
leading-zero loss). Also dedupes across the combined inputs by EIN.

The emailed file is a single column of EINs (a header row "EIN" is auto-skipped).

Example:
    python dedupe_emailed.py --input D:/Run/goodfeel.csv --input D:/Run/no_ntee.csv \
      --emailed-file D:/Run/emailed_eins.csv \
      --output D:/Run/final.csv --overlap-output D:/Run/overlap.csv
"""
import argparse, csv
from collections import Counter

csv.field_size_limit(10**9)


def norm_ein(e):
    return (e or "").strip().replace("-", "").upper()


def load_emailed(path):
    eins = []
    with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
        for i, row in enumerate(csv.reader(f)):
            if not row:
                continue
            if i == 0 and norm_ein(row[0]).upper() == "EIN":
                continue
            e = norm_ein(row[0])
            if e:
                eins.append(e)
    return set(eins), len(eins), len(set(eins))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", action="append", required=True, help="repeatable")
    ap.add_argument("--emailed-file", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--overlap-output", required=True)
    args = ap.parse_args()

    emailed, em_rows, em_uniq = load_emailed(args.emailed_file)
    dup_inside = em_rows - em_uniq

    header = None
    seen = set()
    matched = inter_dupe = 0
    feel_c = Counter(); found_c = Counter(); state_c = Counter()
    new_total = 0

    out_f = open(args.output, "w", newline="", encoding="utf-8")
    ov_f = open(args.overlap_output, "w", newline="", encoding="utf-8")
    out_w = ov_w = None

    for path in args.input:
        with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as in_f:
            reader = csv.reader(in_f)
            h = next(reader)
            if header is None:
                header = h
                ein_idx = header.index("EIN")
                feel_idx = header.index("feel_class") if "feel_class" in header else None
                found_idx = header.index("FOUNDATION") if "FOUNDATION" in header else None
                state_idx = header.index("STATE") if "STATE" in header else None
                out_w = csv.writer(out_f); ov_w = csv.writer(ov_f)
                out_w.writerow(header); ov_w.writerow(header)
            for row in reader:
                new_total += 1
                ein = norm_ein(row[ein_idx]) if ein_idx < len(row) else ""
                if ein and ein in seen:
                    inter_dupe += 1; continue
                if ein:
                    seen.add(ein)
                if ein and ein in emailed:
                    matched += 1; ov_w.writerow(row); continue
                out_w.writerow(row)
                if feel_idx is not None and feel_idx < len(row):
                    feel_c[row[feel_idx].strip()] += 1
                if found_idx is not None and found_idx < len(row):
                    found_c[row[found_idx].strip()] += 1
                if state_idx is not None and state_idx < len(row):
                    state_c[row[state_idx].strip()] += 1
    out_f.close(); ov_f.close()

    fresh = sum(feel_c.values()) if feel_c else (new_total - matched - inter_dupe)
    rate = matched / new_total * 100 if new_total else 0
    print("=" * 56)
    print("DEDUPE vs PREVIOUSLY-EMAILED")
    print("=" * 56)
    print(f"Emailed file rows : {em_rows:,} | unique EINs: {em_uniq:,}"
          + (f"  (*** {dup_inside:,} dup inside)" if dup_inside else "  (no dups inside)"))
    print(f"Combined input rows: {new_total:,}")
    print(f"Cross-input dup EINs dropped: {inter_dupe:,}")
    print(f"Matched/removed (already emailed): {matched:,}")
    print(f"FRESH count       : {fresh:,}")
    print(f"Match rate        : {rate:.2f}%")
    if feel_c:
        print("Fresh by feel_class:", dict(feel_c))
    if found_c:
        print("Fresh by FOUNDATION:", dict(sorted(found_c.items())))
    if state_c:
        print("Top 10 states:", [f"{s}:{n}" for s, n in state_c.most_common(10)])
    print(f"Output: {args.output} | overlap: {args.overlap_output}")
    print("=" * 56)


if __name__ == "__main__":
    main()
