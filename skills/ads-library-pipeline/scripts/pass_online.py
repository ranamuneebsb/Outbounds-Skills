#!/usr/bin/env python3
"""pass_online.py — web-search RESCUE of rows the copy-only classifier couldn't call.

Pass A: rows the classifier marked "Review" (mixed/unclear from copy alone).
Pass B: rows marked "error" (no usable ad copy) — excludes story-farm junk.
Both search the company name to learn who they actually sell to, resolve to
Qualified/Disqualified against the SAME user-provided ICP prompt, and return any
official root domain found (with a conflict flag vs the ad-link domain).

A and B are independent and run IN PARALLEL in the pipeline.

    python pass_online.py --workdir D:/c/acme --icp-prompt D:/c/acme/icp.md --pass both --write

Model (override --model): SEARCH_MODEL = openai/gpt-4o-mini:online
API key: ~/.olympolabs/openrouter.json only.
"""
import csv, os, json, re, time, argparse, threading, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import requests
from classify_icp import build_system, build_user, load_key

csv.field_size_limit(10**9)

SEARCH_MODEL = "openai/gpt-4o-mini:online"
URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = load_key("openrouter")

PASS_A_EXTRA = """

You are RE-EVALUATING an advertiser previously marked "Review" (mixed/unclear from ad copy alone).
USE WEB SEARCH: search the company name to learn who they actually sell to, then make a firm call.
Resolve to Qualified (>=7) or Disqualified (<=4). Only use 5-6 if they GENUINELY serve businesses and consumers equally.
Also return the company's official website root domain you find (domain_found), or "" if none.
Return STRICT JSON only: {"icp_score": <1-10 or null>, "niche": "<one niche>", "reasoning": "<one line incl. what search showed>", "domain_found": "<root domain or empty>"}"""

PASS_B_EXTRA = """

This advertiser had no usable ad copy. USE WEB SEARCH on the company name (and page_category) to identify what they do
and who they sell to, then classify against the ICP. If you genuinely cannot find them, return icp_score null / niche ERROR.
Also return the company's official website root domain you find (domain_found), or "" if none.
Return STRICT JSON only: {"icp_score": <1-10 or null>, "niche": "<one niche>", "reasoning": "<one line incl. what search showed>", "domain_found": "<root domain or empty>"}"""

JUNK = ("fiction", "narrative", "novel", "personal story", "story-farm")


def root(host):
    if not host:
        return ""
    host = host.strip().lower()
    if "//" not in host:
        host = "http://" + host
    try:
        h = urlparse(host).netloc.lower()
    except Exception:
        return ""
    if not h:
        h = host.split("/")[0]
    if h.startswith("www."):
        h = h[4:]
    parts = h.split(":")[0].split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else parts[0]


def extract_json(txt):
    txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.I | re.M).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else json.loads(txt)


def call(row, system, extra, model):
    body = {"model": model, "temperature": 0, "usage": {"include": True},
            "plugins": [{"id": "web", "max_results": 3}],
            "messages": [{"role": "system", "content": system + extra},
                         {"role": "user", "content": build_user(row)}]}
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    attempts = 0
    while True:
        try:
            r = requests.post(URL, headers=headers, json=body, timeout=180)
        except Exception as e:
            attempts += 1
            if attempts > 2:
                return None, 0.0, f"network error: {e}"
            time.sleep(2 * attempts); continue
        if r.status_code == 429:
            attempts += 1
            if attempts > 5:
                return None, 0.0, "persistent 429"
            time.sleep(min(2 ** attempts, 30)); continue
        if r.status_code != 200:
            return None, 0.0, f"http {r.status_code}: {r.text[:120]}"
        j = r.json()
        cost = (j.get("usage") or {}).get("cost", 0.0) or 0.0
        try:
            return extract_json(j["choices"][0]["message"]["content"]), cost, None
        except Exception as e:
            return None, cost, f"json parse: {e}"


def finalize(obj, err, existing_domain):
    if err or obj is None:
        return dict(icp_score="", icp_verdict="error", niche="ERROR",
                    icp_reasoning=err or "no result", domain_found="", domain_conflict="")
    score, niche, reason = obj.get("icp_score"), obj.get("niche", "ERROR"), obj.get("reasoning", "")
    dfound, ex = root(obj.get("domain_found", "")), root(existing_domain)
    conflict = "TRUE" if (dfound and ex and dfound != ex) else ""
    if score is None or niche == "ERROR":
        return dict(icp_score="", icp_verdict="error", niche="ERROR",
                    icp_reasoning=reason or "cannot classify", domain_found=dfound, domain_conflict=conflict)
    s = int(round(float(score)))
    verdict = "Qualified" if s >= 7 else ("Disqualified" if s <= 4 else "Review")
    return dict(icp_score=s, icp_verdict=verdict, niche=niche, icp_reasoning=reason,
                domain_found=dfound, domain_conflict=conflict)


def db(path):
    c = sqlite3.connect(path, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS o(k TEXT PRIMARY KEY, score TEXT, verdict TEXT, niche TEXT, reason TEXT, dfound TEXT, conflict TEXT)")
    return c


def base_key(row):
    return (row.get("page_id") or row.get("page_url") or row.get("page_name") or "").strip()


def pool_for(rows, p):
    if p == "A":
        return [r for r in rows if r.get("icp_verdict") == "Review"]
    return [r for r in rows if r.get("icp_verdict") == "error"
            and not any(j in (r.get("icp_reasoning") or "").lower() for j in JUNK)]


def run_pass(rows, p, system, conn, workers, model):
    extra = PASS_A_EXTRA if p == "A" else PASS_B_EXTRA
    cur = conn.cursor()
    have = {k for (k,) in cur.execute("SELECT k FROM o")}
    work = [r for r in pool_for(rows, p) if f"{p}|{base_key(r)}" not in have]
    print(f"Pass {p}: pool={len(pool_for(rows,p))} todo={len(work)}", flush=True)
    cost_total, done, lock = [0.0], [0], threading.Lock()

    def job(row):
        obj, cost, err = call(row, system, extra, model)
        return row, finalize(obj, err, row.get("domain", "")), cost

    if work:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(job, r) for r in work]):
                row, res, cost = f.result()
                cur.execute("INSERT OR REPLACE INTO o VALUES(?,?,?,?,?,?,?)",
                            (f"{p}|{base_key(row)}", str(res['icp_score']), res['icp_verdict'],
                             res['niche'], res['icp_reasoning'], res['domain_found'], res['domain_conflict']))
                with lock:
                    cost_total[0] += cost; done[0] += 1
                    if done[0] % 25 == 0:
                        conn.commit(); print(f"  {p}: {done[0]}/{len(work)}  ${cost_total[0]:.2f}", flush=True)
        conn.commit()
    print(f"Pass {p} done. calls this run={done[0]} cost=${cost_total[0]:.4f}", flush=True)
    return cost_total[0]


def write_back(rows, src, conn):
    cur = conn.cursor()
    cache = {k: (s, v, n, rs, df, cf) for k, s, v, n, rs, df, cf in cur.execute("SELECT * FROM o")}
    FIELDS = list(rows[0].keys())
    for col in ("domain_found", "domain_conflict"):
        if col not in FIELDS:
            FIELDS.append(col)
    updated = 0
    for r in rows:
        r.setdefault("domain_found", ""); r.setdefault("domain_conflict", "")
        bk = base_key(r)
        hit = cache.get(f"A|{bk}") or cache.get(f"B|{bk}")
        if hit:
            s, v, n, rs, df, cf = hit
            r["icp_score"], r["icp_verdict"], r["niche"], r["icp_reasoning"] = s, v, n, rs
            r["domain_found"], r["domain_conflict"] = df, cf
            updated += 1
    with open(src, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"Wrote back {updated} resolved rows to {src}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--icp-prompt", "--icp", dest="icp", required=True,
                    help="Same USER-PROVIDED ICP prompt file used by classify_icp.py")
    ap.add_argument("--pass", dest="p", choices=["A", "B", "both"], required=True)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--model", default=SEARCH_MODEL)
    ap.add_argument("--cache-dir", default="")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    DIR = args.workdir
    SRC = os.path.join(DIR, "advertisers_clean.csv")
    DB = os.path.join(args.cache_dir or DIR, "online_cache.sqlite")
    system = build_system(open(args.icp, encoding="utf-8").read())

    rows = list(csv.DictReader(open(SRC, encoding="utf-8", newline="")))
    conn = db(DB); total = 0.0
    for p in (["A", "B"] if args.p == "both" else [args.p]):
        total += run_pass(rows, p, system, conn, args.workers, args.model)
    print(f"\nTOTAL search cost this run: ${total:.4f}", flush=True)
    if args.write:
        write_back(rows, SRC, conn)


if __name__ == "__main__":
    main()
