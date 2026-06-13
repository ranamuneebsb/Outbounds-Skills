"""
Step 1 - Download IRS Exempt-Organizations (EO BMF) state CSVs.

Downloads every state file (+ DC) from the IRS SOI EO Business Master File:
    {base-url}/eo_{st}.csv      e.g. https://www.irs.gov/pub/irs-soi/eo_ca.csv

Resume-safe (skips files already present with size > 0), sequential with a polite
delay, prints rows + size per state.

Example:
    python download_states.py --out-dir D:/Run/raw
    python download_states.py --out-dir D:/Run/raw --states ca,ny,tx --delay 2
"""
import argparse, os, sys, time
import urllib.request

DEFAULT_BASE = "https://www.irs.gov/pub/irs-soi"
ALL_STATES = [
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo",
    "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa",
    "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) nonprofit-orgs-classification/1.0"


def count_rows(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return max(sum(1 for _ in f) - 1, 0)
    except Exception:
        return -1


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def download(st, base, out_dir):
    url = f"{base}/eo_{st}.csv"
    dest = os.path.join(out_dir, f"eo_{st}.csv")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  [skip] {st.upper():<3} present  rows={count_rows(dest):>8}  size={human(os.path.getsize(dest))}")
        return True, False
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        with open(dest, "wb") as f:
            f.write(data)
        print(f"  [ok]   {st.upper():<3} got      rows={count_rows(dest):>8}  size={human(len(data))}")
        return True, True
    except Exception as e:
        print(f"  [FAIL] {st.upper():<3} {url} -> {e}")
        return False, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--states", default="", help="comma list (default: all 50 + DC)")
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    states = [s.strip().lower() for s in args.states.split(",") if s.strip()] or ALL_STATES
    os.makedirs(args.out_dir, exist_ok=True)
    print(f"Downloading {len(states)} IRS EO files into {args.out_dir}\n")

    ok, failed = 0, []
    for i, st in enumerate(states, 1):
        print(f"[{i:>2}/{len(states)}]", end=" ")
        success, hit_net = download(st, args.base_url, args.out_dir)
        ok += 1 if success else 0
        if not success:
            failed.append(st)
        if hit_net and i < len(states):
            time.sleep(args.delay)

    print(f"\nDone. {ok}/{len(states)} present.")
    if failed:
        print("Failed (re-run to retry):", ", ".join(s.upper() for s in failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
