"""
Step 3 - Configurable ICP filter over the merged BMF.

Keeps rows where ALL provided conditions hold. Every condition is optional and
user-supplied per run - nothing is hardcoded.

Conditions:
  --subsection 03            exact match on SUBSECTION (e.g. 03 = 501(c)(3))
  --deductibility 1          exact match on DEDUCTIBILITY
  --foundation-codes 10,11,15  FOUNDATION must be one of these
  --min-revenue 800000       REVENUE_AMT >= floor (blank/non-numeric excluded & counted)
  --max-revenue 0            optional ceiling (0/unset = no ceiling)
  --revenue-field REVENUE_AMT  which numeric column to threshold on
  --keep-columns EIN,NAME,...  output only these columns (default: all)

Example:
    python filter_icp.py --input D:/Run/merged_all_states.csv --output D:/Run/icp.csv \
      --subsection 03 --deductibility 1 --foundation-codes 10,11,15 --min-revenue 800000 \
      --keep-columns EIN,NAME,STREET,CITY,STATE,ZIP,FOUNDATION,NTEE_CD,REVENUE_AMT,ASSET_AMT
"""
import argparse, csv
from collections import Counter

csv.field_size_limit(10**9)


def parse_amount(raw):
    s = (raw or "").strip()
    if not s:
        return None
    for c in (",", "$"):
        s = s.replace(c, "")
    try:
        return float(s)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--subsection", default=None)
    ap.add_argument("--deductibility", default=None)
    ap.add_argument("--foundation-codes", default="", help="comma list, e.g. 10,11,15")
    ap.add_argument("--min-revenue", type=float, default=None)
    ap.add_argument("--max-revenue", type=float, default=None)
    ap.add_argument("--revenue-field", default="REVENUE_AMT")
    ap.add_argument("--keep-columns", default="", help="comma list (default: all)")
    args = ap.parse_args()

    foundations = {c.strip() for c in args.foundation_codes.split(",") if c.strip()}
    keep = [c.strip() for c in args.keep_columns.split(",") if c.strip()]

    kept = 0
    per_state = Counter()
    by_foundation = Counter()
    excluded_blank_zero = 0  # passed all non-revenue filters but blank/zero revenue

    with open(args.input, "r", newline="", encoding="utf-8-sig", errors="replace") as in_f, \
            open(args.output, "w", newline="", encoding="utf-8") as out_f:
        reader = csv.DictReader(in_f)
        out_cols = keep or reader.fieldnames
        writer = csv.writer(out_f)
        writer.writerow(out_cols)

        for row in reader:
            if args.subsection is not None and row.get("SUBSECTION", "").strip() != args.subsection:
                continue
            if args.deductibility is not None and row.get("DEDUCTIBILITY", "").strip() != args.deductibility:
                continue
            if foundations and row.get("FOUNDATION", "").strip() not in foundations:
                continue
            # revenue gate
            if args.min_revenue is not None or args.max_revenue:
                amt = parse_amount(row.get(args.revenue_field))
                if amt is None or amt <= 0:
                    excluded_blank_zero += 1
                    continue
                if args.min_revenue is not None and amt < args.min_revenue:
                    continue
                if args.max_revenue and amt > args.max_revenue:
                    continue
            writer.writerow([row.get(c, "") for c in out_cols])
            kept += 1
            per_state[row.get("STATE", "").strip()] += 1
            if foundations:
                by_foundation[row.get("FOUNDATION", "").strip()] += 1

    print("=" * 52)
    print("ICP FILTER")
    print("=" * 52)
    print(f"Filters: subsection={args.subsection} deductibility={args.deductibility} "
          f"foundation={sorted(foundations) or 'any'} min_rev={args.min_revenue} max_rev={args.max_revenue or 'none'}")
    print(f"NATIONAL TOTAL (qualified): {kept:,}")
    if by_foundation:
        print("By FOUNDATION code:")
        for c in sorted(by_foundation):
            print(f"  {c}: {by_foundation[c]:,}")
    print("Top 15 states:")
    for st, n in per_state.most_common(15):
        print(f"  {st:<4} {n:>8,}")
    if args.min_revenue is not None or args.max_revenue:
        print(f"Excluded for blank/zero {args.revenue_field}: {excluded_blank_zero:,}")
    print(f"Output: {args.output}")
    print("=" * 52)


if __name__ == "__main__":
    main()
