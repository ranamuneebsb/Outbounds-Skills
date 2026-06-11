#!/usr/bin/env python3
"""classify_icp.py — score each advertiser against the campaign ICP from AD COPY.

The ICP is NEVER hardcoded. --icp points at the campaign's own icp.md, which is
injected into a fixed scaffold (build_system) that defines the JSON output, the
score->verdict mapping, and the "don't guess -> null/ERROR" rule. Classify runs
on ad copy BEFORE any paid domain lookup or scraping.

Models (override with --model / --fallback):
    CLASSIFY_MODEL    = google/gemini-2.5-flash-lite   (primary)
    CLASSIFY_FALLBACK = openai/gpt-4o-mini             (per-row fallback on hard error)

Pilot 100 rows before every full run:
    python classify_icp.py --workdir D:/c/acme --icp D:/c/acme/icp.md --limit 100 --cost-compare
The --cost-compare pilot runs BOTH models on the pilot rows and prints the REAL
billed cost (usage.include) of each plus their verdict agreement, so you can see
whether Gemini's hidden reasoning tokens make it pricier than gpt-4o-mini and
fall back if so. Then the full run:
    python classify_icp.py --workdir D:/c/acme --icp D:/c/acme/icp.md --write

API key: ~/.olympolabs/openrouter.json only. Never env, never hardcoded.
"""
import csv, os, json, sqlite3, time, threading, argparse, random, hashlib, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import requests

csv.field_size_limit(10**9)

CLASSIFY_MODEL = "google/gemini-2.5-flash-lite"
CLASSIFY_FALLBACK = "openai/gpt-4o-mini"
URL = "https://openrouter.ai/api/v1/chat/completions"


def load_key(name):
    with open(os.path.expanduser(f"~/.olympolabs/{name}.json")) as f:
        return json.load(f)["api_key"]


API_KEY = load_key("openrouter")

SYSTEM_HEAD = ("You classify advertisers from their Meta ad copy. You judge WHO THE AD IS "
               "SPEAKING TO (its intended customer), not who runs the page.\n\n")
SYSTEM_TAIL = """

SCORING: 7-10 = QUALIFIES. 1-4 = DISQUALIFY. 5-6 = genuinely MIXED (the ad speaks to a qualifying audience AND a disqualifying one roughly equally).
If the copy is simply UNREADABLE / too thin / ambiguous / in a language you cannot read — DO NOT GUESS and DO NOT use 5-6. Instead return {"icp_score": null, "niche": "ERROR", "reasoning": "cannot classify: <why>"}.

Return STRICT JSON only: {"icp_score": <1-10 or null>, "niche": "<one niche from the list above>", "reasoning": "<one line>"}"""


def build_system(icp_text):
    return SYSTEM_HEAD + icp_text.strip() + SYSTEM_TAIL


def build_user(row):
    return (f"page_name: {row.get('page_name','')}\n"
            f"page_category: {row.get('page_category','')}\n"
            f"ad_titles: {row.get('ad_titles','')}\n"
            f"ad_copy_full: {row.get('ad_copy_full','')}")


def extract_json(txt):
    txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.I | re.M).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else json.loads(txt)


class Gate:
    def __init__(self, n):
        self.sem = threading.Semaphore(n); self.n = n; self.reduced = False; self.lock = threading.Lock()
    def acquire(self): self.sem.acquire()
    def release(self): self.sem.release()
    def reduce_to(self, target):
        with self.lock:
            if self.reduced:
                return
            self.reduced = True
            drop = self.n - target
        for _ in range(max(drop, 0)):
            self.sem.acquire()
        print(f"  [rate-limit] reduced concurrency {self.n} -> {target}")


_429 = {"n": 0}
_429lock = threading.Lock()


def err(msg):
    return {"icp_score": "", "icp_verdict": "error", "niche": "ERROR", "icp_reasoning": msg, "cost": 0.0}


def finalize(obj, cost):
    score = obj.get("icp_score", None)
    niche = obj.get("niche", "ERROR")
    reason = obj.get("reasoning", "")
    if score is None or niche == "ERROR":
        return {"icp_score": "", "icp_verdict": "error", "niche": "ERROR",
                "icp_reasoning": reason or "cannot classify", "cost": cost}
    try:
        s = int(round(float(score)))
    except Exception:
        return {**err(f"bad score: {score}"), "cost": cost}
    verdict = "Qualified" if s >= 7 else ("Disqualified" if s <= 4 else "Review")
    return {"icp_score": s, "icp_verdict": verdict, "niche": niche, "icp_reasoning": reason, "cost": cost}


def call_llm(row, system, model, gate):
    payload = {"model": model, "temperature": 0, "usage": {"include": True},
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": build_user(row)}]}
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    attempts = 0
    while True:
        gate.acquire()
        try:
            r = requests.post(URL, headers=headers, json=payload, timeout=90)
        except Exception as e:
            gate.release(); attempts += 1
            if attempts > 2:
                return err(f"network error: {e}")
            time.sleep(2 * attempts); continue
        gate.release()
        if r.status_code == 429:
            with _429lock:
                _429["n"] += 1; tot = _429["n"]
            if tot >= 15:
                gate.reduce_to(60)
            attempts += 1
            if attempts > 6:
                return err("persistent 429")
            time.sleep(min(2 ** attempts, 30) + random.uniform(0, 1)); continue
        if r.status_code >= 500:
            attempts += 1
            if attempts > 2:
                return err(f"http {r.status_code}")
            time.sleep(2 * attempts); continue
        if r.status_code != 200:
            return err(f"http {r.status_code}: {r.text[:120]}")
        try:
            j = r.json()
            cost = (j.get("usage") or {}).get("cost", 0.0) or 0.0
            obj = extract_json(j["choices"][0]["message"]["content"])
        except Exception as e:
            attempts += 1
            if attempts > 2:
                return err(f"json parse: {e}")
            continue
        return finalize(obj, cost)


def db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS r(k TEXT PRIMARY KEY, score TEXT, verdict TEXT, niche TEXT, reason TEXT)")
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--icp-prompt", "--icp", dest="icp", required=True,
                    help="Path to the campaign's ICP/classification prompt file. "
                         "USER-PROVIDED EVERY RUN. Never hardcoded, never reused across campaigns.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=100)
    ap.add_argument("--model", default=CLASSIFY_MODEL)
    ap.add_argument("--fallback", default=CLASSIFY_FALLBACK)
    ap.add_argument("--cost-compare", action="store_true",
                    help="Pilot: run BOTH --model and --fallback, report billed cost + agreement, write nothing")
    ap.add_argument("--cache-dir", default="", help="Where to keep icp_cache.sqlite (default workdir; share for cross-campaign reuse)")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    DIR = args.workdir
    SRC = os.path.join(DIR, "advertisers_clean.csv")
    DB = os.path.join(args.cache_dir or DIR, "icp_cache.sqlite")

    icp_text = open(args.icp, encoding="utf-8").read()
    SYSTEM = build_system(icp_text)
    sys_hash = hashlib.md5(SYSTEM.encode("utf-8")).hexdigest()[:6]  # prompt-hash -> cache invalidation

    def cache_key(row, model):
        base = (row.get("page_id") or row.get("page_url") or row.get("page_name") or "").strip()
        h = hashlib.md5(build_user(row).encode("utf-8")).hexdigest()[:8]
        return f"{model}|{sys_hash}|{base}|{h}"

    rows = list(csv.DictReader(open(SRC, encoding="utf-8", newline="")))
    work = rows[:args.limit] if args.limit else rows

    # ---- pilot cost-compare mode: run both models, no cache, no write ----
    if args.cost_compare:
        pilot = [r for r in work if (r.get("ad_copy_full") or "").strip()]
        gate = Gate(args.workers)
        out = {}
        for model in (args.model, args.fallback):
            res = {}
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(call_llm, r, SYSTEM, model, gate): i for i, r in enumerate(pilot)}
                for f in as_completed(futs):
                    res[futs[f]] = f.result()
            out[model] = res
            tot = sum(v["cost"] for v in res.values())
            n = len(res) or 1
            print(f"\n=== {model} ===  rows={len(res)}  billed=${tot:.4f}  per-row=${tot/n:.5f}  ({time.time()-t0:.0f}s)")
            dist = Counter(v["icp_verdict"] for v in res.values())
            print("  verdicts:", dict(dist))
        a, b = args.model, args.fallback
        agree = sum(1 for i in range(len(pilot))
                    if out[a].get(i, {}).get("icp_verdict") == out[b].get(i, {}).get("icp_verdict"))
        ca = sum(v["cost"] for v in out[a].values())
        cb = sum(v["cost"] for v in out[b].values())
        print(f"\nVerdict agreement {a} vs {b}: {agree}/{len(pilot)} ({agree/max(len(pilot),1)*100:.0f}%)")
        cheaper = a if ca <= cb else b
        print(f"REAL billed cost: {a}=${ca:.4f}  {b}=${cb:.4f}  ->  cheaper: {cheaper}")
        if cheaper != a:
            print(f"  WARNING: primary ({a}) billed MORE than fallback ({b}) — "
                  f"hidden reasoning tokens. Run the full pass with  --model {b}")
        return

    conn = db(DB); cur = conn.cursor()
    cached = {k: (s, v, n, rs) for k, s, v, n, rs in cur.execute("SELECT * FROM r")}

    todo = []
    for row in work:
        k = cache_key(row, args.model)
        if k in cached:
            continue
        if not (row.get("ad_copy_full") or "").strip():
            cur.execute("INSERT OR REPLACE INTO r VALUES(?,?,?,?,?)", (k, "", "error", "ERROR", "no ad copy"))
            cached[k] = ("", "error", "ERROR", "no ad copy")
            continue
        todo.append((k, row))
    conn.commit()

    print(f"Model: {args.model} (fallback {args.fallback})")
    print(f"Rows: {len(work)} | cached: {len(work)-len(todo)} | to classify: {len(todo)}")
    gate = Gate(args.workers)
    done = 0; t0 = time.time(); total_cost = 0.0

    def job(k, row):
        res = call_llm(row, SYSTEM, args.model, gate)
        if res["icp_verdict"] == "error" and "http" in (res["icp_reasoning"] or ""):
            res = call_llm(row, SYSTEM, args.fallback, gate)  # per-row fallback on hard error
        return k, res

    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(job, k, row) for k, row in todo]
            for f in as_completed(futs):
                k, res = f.result()
                cur.execute("INSERT OR REPLACE INTO r VALUES(?,?,?,?,?)",
                            (k, str(res["icp_score"]), res["icp_verdict"], res["niche"], res["icp_reasoning"]))
                cached[k] = (str(res["icp_score"]), res["icp_verdict"], res["niche"], res["icp_reasoning"])
                done += 1; total_cost += res.get("cost", 0.0)
                if done % 25 == 0:
                    conn.commit(); print(f"  {done}/{len(todo)}  ({done/(time.time()-t0):.1f}/s)  ${total_cost:.3f}")
        conn.commit()

    for row in work:
        s, v, n, rs = cached[cache_key(row, args.model)]
        row["icp_score"], row["icp_verdict"], row["niche"], row["icp_reasoning"] = s, v, n, rs

    dist = Counter(r["icp_verdict"] for r in work)
    qual_niche = Counter(r["niche"] for r in work if r["icp_verdict"] == "Qualified")
    print(f"\n===== CLASSIFY REPORT ({len(work)} rows) =====")
    print(f"calls this run: {done}  billed ${total_cost:.4f}  (${total_cost/max(done,1):.5f}/row)")
    for v in ["Qualified", "Review", "Disqualified", "error"]:
        print(f"  {v:<13} {dist.get(v,0)}")
    print(f"\nQualified niche breakdown:")
    for n, c in qual_niche.most_common():
        print(f"  {c:>4}  {n}")
    print(f"\n15 sample rows:")
    for r in work[:15]:
        print(f"  [{str(r['icp_verdict'])[:4]:<4} {str(r['icp_score']):<2}] {str(r['page_name'])[:30]:<30} | {str(r['niche'])[:26]:<26} | {str(r['icp_reasoning'])[:55]}")

    if args.write:
        FIELDS = list(rows[0].keys())
        for c in ("icp_score", "icp_verdict", "niche", "icp_reasoning"):
            if c not in FIELDS:
                FIELDS.append(c)
        for row in rows:
            k = cache_key(row, args.model)
            if k in cached:
                s, v, n, rs = cached[k]
                row["icp_score"], row["icp_verdict"], row["niche"], row["icp_reasoning"] = s, v, n, rs
            else:
                for c in ("icp_score", "icp_verdict", "niche", "icp_reasoning"):
                    row.setdefault(c, "")
        with open(SRC, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
        print(f"\nWrote columns back to {SRC}")


if __name__ == "__main__":
    main()
