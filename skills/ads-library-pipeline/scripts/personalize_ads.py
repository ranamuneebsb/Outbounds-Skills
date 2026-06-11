#!/usr/bin/env python3
"""personalize_ads.py — one personalization line per Qualified advertiser, ONE pass.

The personalization prompt is USER-PROVIDED every run (--personalize-prompt file):
it carries the format rules, framing, and examples. The script wraps it with a
fixed JSON-output scaffold and runs a single pass that emits the FINAL ready-to-use
line (e.g. the "your X" format) directly — there is no separate reformat step.

Pilot first (prints samples, writes nothing):
    python personalize_ads.py --workdir D:/c/acme --personalize-prompt D:/c/acme/pers.md --limit 20
Then the full write:
    python personalize_ads.py --workdir D:/c/acme --personalize-prompt D:/c/acme/pers.md --write

Reads qualified_advertisers.csv, adds an `ads_promote` column in place.
Model (override --model): PERSONALIZE_MODEL = anthropic/claude-haiku-4.5
Cache key = page_id/domain + prompt-hash, so a changed prompt => fresh lines.
API key: ~/.olympolabs/openrouter.json only.
"""
import csv, os, json, re, time, argparse, threading, sqlite3, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

csv.field_size_limit(10**9)

PERSONALIZE_MODEL = "anthropic/claude-haiku-4.5"
URL = "https://openrouter.ai/api/v1/chat/completions"


def load_key(name):
    with open(os.path.expanduser(f"~/.olympolabs/{name}.json")) as f:
        return json.load(f)["api_key"]


API_KEY = load_key("openrouter")

SCAFFOLD_TAIL = """

OUTPUT CONTRACT (fixed — do not deviate):
- Produce the FINAL, ready-to-paste line in ONE pass (already in the exact format the rules above specify; no placeholder, no second step).
- Plain text, no quotes, no emoji, no company name unless the rules above explicitly ask for it.
- Never return empty: if the ad copy is too thin, fall back to a niche-accurate line per the rules above.
Return STRICT JSON only: {"ads_promote": "<the final line>"}"""


def build_system(prompt_text):
    return prompt_text.strip() + SCAFFOLD_TAIL


def extract_json(txt):
    txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.I | re.M).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else json.loads(txt)


def user_content(a):
    return (f"page_name: {a.get('page_name','')}\n"
            f"niche: {a.get('niche','')}\n"
            f"domain: {a.get('domain','')}\n"
            f"ad_titles: {a.get('ad_titles','')}\n"
            f"ad_copy_full: {(a.get('ad_copy_full','') or '')[:1500]}")


def call(a, system, model):
    body = {"model": model, "temperature": 0, "usage": {"include": True}, "max_tokens": 90,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user_content(a)}]}
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    attempts = 0
    while True:
        try:
            r = requests.post(URL, headers=headers, json=body, timeout=120)
        except Exception as e:
            attempts += 1
            if attempts > 2:
                return "", 0.0, f"network error: {e}"
            time.sleep(2 * attempts); continue
        if r.status_code == 429:
            attempts += 1
            if attempts > 5:
                return "", 0.0, "429"
            time.sleep(min(2 ** attempts, 30)); continue
        if r.status_code != 200:
            return "", 0.0, f"http {r.status_code}"
        j = r.json()
        cost = (j.get("usage") or {}).get("cost", 0.0) or 0.0
        try:
            line = (extract_json(j["choices"][0]["message"]["content"]).get("ads_promote", "") or "").strip()
        except Exception as e:
            return "", cost, f"json parse: {e}"
        return line, cost, None


def db(path):
    c = sqlite3.connect(path, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS p(k TEXT PRIMARY KEY, line TEXT)")
    return c


def base_key(r):
    return (r.get("page_id") or r.get("domain") or r.get("page_name") or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--personalize-prompt", dest="prompt", required=True,
                    help="USER-PROVIDED personalization prompt file (format rules, framing, examples)")
    ap.add_argument("--input", default="qualified_advertisers.csv")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=60)
    ap.add_argument("--model", default=PERSONALIZE_MODEL)
    ap.add_argument("--cache-dir", default="")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    DIR = args.workdir
    SRC = args.input if os.path.isabs(args.input) else os.path.join(DIR, args.input)
    DB = os.path.join(args.cache_dir or DIR, "personalize_cache.sqlite")

    system = build_system(open(args.prompt, encoding="utf-8").read())
    phash = hashlib.md5(system.encode("utf-8")).hexdigest()[:6]  # prompt-hash -> cache invalidation

    def ckey(r):
        return f"{phash}|{base_key(r)}"

    rows = list(csv.DictReader(open(SRC, encoding="utf-8", newline="")))
    conn = db(DB); cur = conn.cursor()
    have = {k for (k,) in cur.execute("SELECT k FROM p")}
    work = [r for r in rows if base_key(r) and ckey(r) not in have]
    if args.limit:
        work = work[:args.limit]
    print(f"rows: {len(rows)} | to personalize this run: {len(work)} | model {args.model}", flush=True)

    cost, done, lock = [0.0], [0], threading.Lock()

    def job(r):
        line, c, err = call(r, system, args.model)
        return r, line, c

    if work:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for f in as_completed([ex.submit(job, r) for r in work]):
                r, line, c = f.result()
                cur.execute("INSERT OR REPLACE INTO p VALUES(?,?)", (ckey(r), line))
                with lock:
                    cost[0] += c; done[0] += 1
                    if done[0] % 25 == 0:
                        conn.commit(); print(f"  {done[0]}/{len(work)} ${cost[0]:.3f}", flush=True)
        conn.commit()
    cache = {k: l for k, l in cur.execute("SELECT * FROM p")}

    if args.limit and not args.write:
        print(f"\n=== PILOT: {len(work)} lines (cost ${cost[0]:.4f}, ${cost[0]/max(done[0],1):.4f}/row) ===", flush=True)
        for r in work:
            print(f"  [{str(r.get('niche',''))[:22]:<22}] {str(r.get('page_name',''))[:26]:<26} -> {cache.get(ckey(r),'')}", flush=True)
        return

    if args.write:
        FIELDS = list(rows[0].keys())
        if "ads_promote" not in FIELDS:
            FIELDS.append("ads_promote")
        for r in rows:
            r["ads_promote"] = cache.get(ckey(r), "")
        with open(SRC, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
        filled = sum(1 for r in rows if r.get("ads_promote"))
        print(f"\n=== DONE === ads_promote written on {filled}/{len(rows)} rows | cost this run ${cost[0]:.4f}", flush=True)
        print(f"  wrote -> {SRC}", flush=True)
        for r in [r for r in rows if r.get("ads_promote")][:10]:
            print(f"  {str(r.get('page_name',''))[:30]:<30} -> {r['ads_promote']}", flush=True)


if __name__ == "__main__":
    main()
