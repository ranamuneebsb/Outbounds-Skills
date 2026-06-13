"""
Step 4 - Configurable NTEE "noble-cause" classifier.

Reads ALL rules from a user-supplied JSON config (NOT hardcoded). Classifies each
row into good_feel / not_good_feel / no_ntee and writes three files. Nothing is
ever dropped silently - no_ntee rows are kept in their own file for later handling.

Per-row logic (mirrors the config):
  1. code3 = first 3 chars of NTEE_CD (upper/strip); letter = first char
  2. If NTEE blank:
       - if FOUNDATION in blank_ntee.foundation_keep -> good_feel
       - else -> no_ntee  (KEPT, never dropped)
  3. OVERRIDES first (code3 level), else LETTER default (first char):
       - drop_override_code3  -> not_good_feel
       - any code3 whose last 2 chars are in drop_suffixes -> not_good_feel
       - keep_override_code3  -> good_feel
       - letter in good_letters -> good_feel ; in drop_letters -> not_good_feel
       - unrecognized leading char -> not_good_feel

Config JSON (see templates/noble-causes-example.json):
  {
    "good_letters": "CDEFGKLMOPQX",
    "drop_letters": "ABHIJNRSTUVWYZ",
    "drop_override_code3": ["E20","E21",...],
    "drop_suffixes": ["01","02","03","05"],
    "keep_override_code3": ["B20","B21","B24","B28"],
    "blank_ntee": {"foundation_keep": ["10"]}
  }

Example:
    python classify_ntee.py --input D:/Run/icp.csv --config client_noble.json \
      --good-output D:/Run/goodfeel.csv --no-ntee-output D:/Run/no_ntee.csv \
      --dropped-output D:/Run/dropped.csv
"""
import argparse, csv, json
from collections import Counter

csv.field_size_limit(10**9)


def code3_of(ntee):
    return (ntee or "").strip().upper()[:3]


def make_classifier(cfg):
    good = set(cfg.get("good_letters", ""))
    drop = set(cfg.get("drop_letters", ""))
    drop_codes = set(cfg.get("drop_override_code3", []))
    keep_codes = set(cfg.get("keep_override_code3", []))
    suffixes = set(cfg.get("drop_suffixes", []))
    found_keep = set(cfg.get("blank_ntee", {}).get("foundation_keep", []))

    def classify(ntee_raw, foundation):
        ntee = (ntee_raw or "").upper().strip()
        if not ntee:
            return ("good_feel", "(blank-keep)") if foundation in found_keep else ("no_ntee", "(blank)")
        c3, letter = ntee[:3], ntee[0]
        if c3 in drop_codes:
            return "not_good_feel", c3
        if len(c3) == 3 and c3[-2:] in suffixes:
            return "not_good_feel", c3
        if c3 in keep_codes:
            return "good_feel", c3
        if letter in good:
            return "good_feel", c3
        if letter in drop:
            return "not_good_feel", c3
        return "not_good_feel", c3
    return classify


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--config", required=True, help="noble-causes rules JSON")
    ap.add_argument("--good-output", required=True)
    ap.add_argument("--no-ntee-output", required=True)
    ap.add_argument("--dropped-output", required=True)
    ap.add_argument("--good-label", default="good_feel")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    classify = make_classifier(cfg)

    with open(args.input, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        out_cols = reader.fieldnames + ["feel_class"]
        gf = open(args.good_output, "w", newline="", encoding="utf-8")
        nf = open(args.no_ntee_output, "w", newline="", encoding="utf-8")
        df = open(args.dropped_output, "w", newline="", encoding="utf-8")
        gw, nw, dw = (csv.DictWriter(x, fieldnames=out_cols) for x in (gf, nf, df))
        for w in (gw, nw, dw):
            w.writeheader()

        counts = Counter(); good_c3 = Counter(); drop_c3 = Counter(); good_found = Counter()
        blank_keep = 0
        for row in reader:
            foundation = (row.get("FOUNDATION", "") or "").strip()
            feel, c3 = classify(row.get("NTEE_CD"), foundation)
            row["feel_class"] = args.good_label if feel == "good_feel" else feel
            counts[feel] += 1
            if feel == "good_feel":
                gw.writerow(row); good_c3[c3] += 1; good_found[foundation] += 1
                if c3 == "(blank-keep)":
                    blank_keep += 1
            elif feel == "no_ntee":
                nw.writerow(row)
            else:
                dw.writerow(row); drop_c3[c3] += 1
        gf.close(); nf.close(); df.close()

    total = sum(counts.values())
    print("=" * 56)
    print("NTEE NOBLE-CAUSE CLASSIFICATION")
    print("=" * 56)
    print(f"Input rows     : {total:,}")
    print(f"  good_feel    : {counts['good_feel']:,}")
    print(f"  not_good_feel: {counts['not_good_feel']:,}")
    print(f"  no_ntee      : {counts['no_ntee']:,}")
    print(f"Blank-NTEE foundation-keep auto-kept: {blank_keep:,}")
    print("\ngood_feel top code3:")
    for c3, n in good_c3.most_common(25):
        print(f"  {c3:<14} {n:>8,}")
    print("\nnot_good_feel top code3:")
    for c3, n in drop_c3.most_common(25):
        print(f"  {c3:<14} {n:>8,}")
    print("\ngood_feel by FOUNDATION:")
    for c in sorted(good_found):
        print(f"  {c}: {good_found[c]:,}")
    print(f"\nFiles: {args.good_output} | {args.no_ntee_output} | {args.dropped_output}")
    print("=" * 56)


if __name__ == "__main__":
    main()
