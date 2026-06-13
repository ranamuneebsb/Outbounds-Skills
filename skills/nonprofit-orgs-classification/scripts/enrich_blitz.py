"""
Step 7 (optional) - BlitzAPI company-search fallback for still-blank websites.

Fills WEBSITE for website-blank rows via BlitzAPI company search (NAME + CITY, US),
with STRICT gating so wrong-company domains don't leak in. Adds blitz_confidence.

  POST {base}/v2/search/companies   header: x-api-key   (key ~/.olympolabs/blitz.json)
  There is NO state filter (only hq.city/country_code) - STATE is used to
  disambiguate the returned results, not to query.

Acceptance gate (tight):
  - result must have a website/domain
  - name-token overlap (ours vs matched name) >= --overlap-min
  - state must match (abbrev-aware: our "AK" == Blitz "Alaska")
blitz_confidence = high if the website host contains a name word, else low
  (low catches parent/other-org domains; note valid acronym domains also read low).

Credits: ~1 per query that returns any result (x-credit-cost header). x-credit-remaining
is a per-window counter, NOT a total balance.

Pilot first:  python enrich_blitz.py --input X --output Y --pilot 20
Full run:     python enrich_blitz.py --input X --output Y
"""
import argparse, csv, os, json, time, re
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

csv.field_size_limit(10**9)
URL_PATH = "/v2/search/companies"

STATES = {  # 2-letter -> full (Blitz returns full names)
    "AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS", "CA": "CALIFORNIA",
    "CO": "COLORADO", "CT": "CONNECTICUT", "DE": "DELAWARE", "DC": "DISTRICT OF COLUMBIA",
    "FL": "FLORIDA", "GA": "GEORGIA", "HI": "HAWAII", "ID": "IDAHO", "IL": "ILLINOIS",
    "IN": "INDIANA", "IA": "IOWA", "KS": "KANSAS", "KY": "KENTUCKY", "LA": "LOUISIANA",
    "ME": "MAINE", "MD": "MARYLAND", "MA": "MASSACHUSETTS", "MI": "MICHIGAN", "MN": "MINNESOTA",
    "MS": "MISSISSIPPI", "MO": "MISSOURI", "MT": "MONTANA", "NE": "NEBRASKA", "NV": "NEVADA",
    "NH": "NEW HAMPSHIRE", "NJ": "NEW JERSEY", "NM": "NEW MEXICO", "NY": "NEW YORK",
    "NC": "NORTH CAROLINA", "ND": "NORTH DAKOTA", "OH": "OHIO", "OK": "OKLAHOMA", "OR": "OREGON",
    "PA": "PENNSYLVANIA", "RI": "RHODE ISLAND", "SC": "SOUTH CAROLINA", "SD": "SOUTH DAKOTA",
    "TN": "TENNESSEE", "TX": "TEXAS", "UT": "UTAH", "VT": "VERMONT", "VA": "VIRGINIA",
    "WA": "WASHINGTON", "WV": "WEST VIRGINIA", "WI": "WISCONSIN", "WY": "WYOMING",
}
STOP = {"INC", "INCORPORATED", "THE", "OF", "AND", "A", "CORP", "CO", "LLC", "LTD",
        "FOUNDATION", "ORG", "ORGANIZATION", "ASSOCIATION", "FUND", "TRUST"}


def norm_ein(e): return (e or "").strip().replace("-", "").upper()


def norm_tokens(s):
    s = re.sub(r"[^A-Za-z0-9 ]", " ", (s or "").upper())
    return [t for t in s.split() if t and t not in STOP]


def name_overlap(a, b):
    A, B = set(norm_tokens(a)), set(norm_tokens(b))
    return len(A & B) / len(A) if A else 0.0


def normalize_url(url):
    url = (url or "").strip().lower()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/").strip()


def state_match(ours, theirs):
    if not theirs:
        return False
    o, b = (ours or "").upper().strip(), theirs.upper().strip()
    return b == o or b == STATES.get(o, "~") or o == b


def domain_confidence(name, website):
    host = re.sub(r"^https?://", "", (website or "").lower()).split("/")[0]
    return "high" if any(t.lower() in host for t in norm_tokens(name) if len(t) >= 4) else "low"


def build_payload(name, city):
    company = {"name": {"include": [name], "exclude": []}}
    hq = {"country_code": ["US"]}
    if city:
        hq["city"] = {"include": [city], "exclude": []}
    company["hq"] = hq
    return {"company": company, "max_results": 5, "cursor": None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--key-file", default=os.path.expanduser("~/.olympolabs/blitz.json"))
    ap.add_argument("--base-url", default="https://api.blitz-api.ai")
    ap.add_argument("--overlap-min", type=float, default=0.6)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--pilot", type=int, default=0)
    args = ap.parse_args()

    api_key = json.load(open(args.key_file, encoding="utf-8"))["api_key"]
    url = args.base_url.rstrip("/") + URL_PATH
    ck = args.checkpoint or (os.path.splitext(args.output)[0] + ".blitz_ckpt.json")

    def search(name, city):
        payload = json.dumps(build_payload(name, city)).encode("utf-8")
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read()), int(resp.headers.get("x-credit-cost", 0) or 0)
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403):
                    return {"_error": e.code}, 0
                time.sleep(2 * (attempt + 1))
            except Exception:
                time.sleep(2 * (attempt + 1))
        return {"_error": "retries"}, 0

    def process(row):
        name, city, state = row.get("NAME", ""), row.get("CITY", ""), row.get("STATE", "")
        body, cost = search(name, city)
        out = {"ein": norm_ein(row.get("EIN")), "website": "", "confidence": "", "matched": "", "cost": cost}
        for r in body.get("results", []) if isinstance(body, dict) else []:
            site = r.get("website") or (("https://" + r["domain"]) if r.get("domain") else "")
            if not site:
                continue
            if name_overlap(name, r.get("name", "")) < args.overlap_min:
                continue
            if not state_match(state, r.get("hq", {}).get("state", "")):
                continue
            out["website"] = normalize_url(site)
            out["confidence"] = domain_confidence(name, site)
            out["matched"] = r.get("name", "")
            break
        return out

    with open(args.input, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.DictReader(f)); fields = list(rows[0].keys())
    blanks = [r for r in rows if not (r.get("WEBSITE") or "").strip()]
    print(f"website-blank: {len(blanks):,}", flush=True)

    if args.pilot:
        sample = blanks[:args.pilot]
        print("EXAMPLE request body:", json.dumps(build_payload(sample[0].get("NAME"), sample[0].get("CITY"))))
        found = 0
        for r in sample:
            o = process(r)
            if o["website"]:
                found += 1
            print(f"  {r.get('NAME')} -> {o['website'] or '(none)'}  [{o['confidence']}] matched={o['matched']}")
        print(f"PILOT: {found}/{len(sample)} found")
        return

    done = json.load(open(ck, encoding="utf-8")) if os.path.exists(ck) else {}
    todo = [r for r in blanks if norm_ein(r.get("EIN")) not in done]
    total_cost = sum(v.get("cost", 0) for v in done.values())
    for s in range(0, len(todo), 100):
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for o in ex.map(process, todo[s:s + 100]):
                done[o["ein"]] = o; total_cost += o.get("cost", 0)
        json.dump(done, open(ck, "w", encoding="utf-8"))
        nf = sum(1 for v in done.values() if v.get("website"))
        print(f"  attempted {len(done):,}/{len(blanks):,} | found {nf:,} | credits {total_cost:,}", flush=True)

    out_fields = fields + (["blitz_confidence"] if "blitz_confidence" not in fields else [])
    nf = nh = nl = 0
    for r in rows:
        r.setdefault("blitz_confidence", "")
        v = done.get(norm_ein(r.get("EIN")))
        if v and v.get("website") and not (r.get("WEBSITE") or "").strip():
            r["WEBSITE"] = v["website"]; r["WEBSITE_SOURCE"] = "blitz"; r["blitz_confidence"] = v["confidence"]
            nf += 1; nh += v["confidence"] == "high"; nl += v["confidence"] == "low"
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields); w.writeheader(); w.writerows(rows)

    total = len(rows); enriched = sum(1 for r in rows if (r.get("WEBSITE_SOURCE") or "").strip())
    print("\n" + "=" * 56)
    print(f"BLITZ FALLBACK: attempted {len(blanks):,} | found {nf:,} (high {nh:,} / low {nl:,})")
    print(f"Credits used {total_cost:,} | coverage now {enriched/total*100:.1f}% | Output {args.output}")
    print("=" * 56)


if __name__ == "__main__":
    main()
