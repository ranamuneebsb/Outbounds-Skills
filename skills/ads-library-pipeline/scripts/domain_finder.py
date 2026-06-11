#!/usr/bin/env python3
"""domain_finder.py — find official domains for QUALIFIED advertisers with no domain.

Ladder per row (cheapest first):
  1. NAME-CLEAN     strip FB-page cruft (LLC/Inc, ' | tagline', emoji) to a clean
                    company name to query with.
  2. BLITZ          ~/.olympolabs/blitz.json domain-lookup API (cheap/bulk). Skipped
                    automatically if blitz.json is absent or the call fails.
  3. ONLINE LLM     SEARCH_MODEL = openai/gpt-4o-mini:online — web search, returns
                    root domain + confidence + evidence. Only high/medium-confidence
                    non-blocklisted domains are written (never guess; trackers and
                    aggregators are not domains).

    python domain_finder.py --workdir D:/c/acme --write

Runs only on icp_verdict==Qualified AND blank domain. Conflicts/low-confidence are
recorded but NOT written over an existing domain. API keys: files only.
"""
import csv, os, json, re, time, argparse, threading, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import requests

csv.field_size_limit(10**9)

SEARCH_MODEL = "openai/gpt-4o-mini:online"
URL = "https://openrouter.ai/api/v1/chat/completions"


def load_key(name):
    p = os.path.expanduser(f"~/.olympolabs/{name}.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p)).get("api_key")
    except Exception:
        return None


API_KEY = load_key("openrouter")

# Blitz config (optional). blitz.json: {"api_key": "...", "base_url": "https://..."}
_BLITZ = {}
try:
    _bp = os.path.expanduser("~/.olympolabs/blitz.json")
    if os.path.exists(_bp):
        _BLITZ = json.load(open(_bp))
except Exception:
    _BLITZ = {}

BLOCK = {"facebook.com", "instagram.com", "linkedin.com", "yelp.com", "twitter.com", "x.com",
         "youtube.com", "tiktok.com", "bbb.org", "crunchbase.com", "glassdoor.com", "indeed.com",
         "google.com", "apple.com", "wikipedia.org", "mapquest.com", "manta.com", "dnb.com",
         "linktr.ee", "whatsapp.com", "doubleclick.net"}

SYSTEM = """You find the official website of a company. Search the web for the company name. Return STRICT JSON only:
{"domain_found": "<root domain or empty>", "confidence": "high/medium/low", "evidence": "<one line: how you know it's them>"}

Rules:
- The company advertises on Facebook; you are given its FB page name, page category, and a sample of its ad copy for context
- Match the company, not a similarly named one - use the ad copy/category to confirm
- Return the ROOT domain only (no www, no paths)
- NEVER return facebook.com, instagram.com, linkedin.com, yelp.com, directories, or news articles about the company
- If you cannot confidently identify their official site, return empty domain with confidence low - do not guess"""

# ---- name-clean (lightweight; strips FB-page cruft before querying) ----
SUFFIX = re.compile(r"\b(llc|l\.l\.c\.|inc|inc\.|incorporated|corp|corp\.|co\.|ltd|ltd\.|gmbh|pllc|pc|llp|plc)\b\.?", re.I)
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF\U0000FE0F]")


def clean_name(name):
    n = EMOJI.sub("", name or "")
    n = re.split(r"\s[|\-–—:]\s", n)[0]      # drop "Brand | tagline"
    n = SUFFIX.sub("", n)
    n = re.sub(r"[®™]", "", n)
    return re.sub(r"\s{2,}", " ", n).strip(" ,.-").strip()


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


def blitz_lookup(name):
    """Optional cheap domain lookup. Returns root domain or '' (never raises)."""
    if not _BLITZ.get("api_key"):
        return ""
    base = _BLITZ.get("base_url", "https://api.blitz.com/v1").rstrip("/")
    try:
        r = requests.get(f"{base}/domain", params={"company": name},
                         headers={"Authorization": f"Bearer {_BLITZ['api_key']}"}, timeout=20)
        if r.status_code != 200:
            return ""
        j = r.json()
        dom = root(j.get("domain") or j.get("website") or "")
        return "" if dom in BLOCK else dom
    except Exception:
        return ""


def user_content(r, name):
    return (f"company_name: {name}\n"
            f"page_category: {r.get('page_category','')}\n"
            f"ad_copy: {(r.get('ad_copy_full','') or '')[:300]}")


def llm_find(row, name):
    body = {"model": SEARCH_MODEL, "temperature": 0, "usage": {"include": True}, "max_tokens": 120,
            "plugins": [{"id": "web", "max_results": 3}],
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user_content(row, name)}]}
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    attempts = 0
    while True:
        try:
            r = requests.post(URL, headers=headers, json=body, timeout=180)
        except Exception as e:
            attempts += 1
            if attempts > 2:
                return {"domain": "", "conf": "low", "ev": f"network error: {e}"}, 0.0
            time.sleep(2 * attempts); continue
        if r.status_code == 429:
            attempts += 1
            if attempts > 5:
                return {"domain": "", "conf": "low", "ev": "persistent 429"}, 0.0
            time.sleep(min(2 ** attempts, 30)); continue
        if r.status_code != 200:
            return {"domain": "", "conf": "low", "ev": f"http {r.status_code}"}, 0.0
        j = r.json()
        cost = (j.get("usage") or {}).get("cost", 0.0) or 0.0
        try:
            o = extract_json(j["choices"][0]["message"]["content"])
        except Exception as e:
            return {"domain": "", "conf": "low", "ev": f"json parse: {e}"}, cost
        dom = root(o.get("domain_found", ""))
        if dom in BLOCK:
            dom = ""
        conf = (o.get("confidence", "low") or "low").lower().strip()
        if conf not in ("high", "medium", "low") or not dom:
            conf = "low"
        return {"domain": dom, "conf": conf, "ev": (o.get("evidence", "") or "")[:200]}, cost


def find_one(row):
    name = clean_name(row.get("page_name", "") or "")
    # 2. Blitz first (cheap)
    bd = blitz_lookup(name) if name else ""
    if bd:
        return {"domain": bd, "conf": "high", "ev": "blitz", "src": "blitz"}, 0.0
    # 3. LLM online fallback
    res, cost = llm_find(row, name or row.get("page_name", ""))
    res["src"] = "name_search" if res["domain"] else "not_found"
    return res, cost


def db(path):
    c = sqlite3.connect(path, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS d(k TEXT PRIMARY KEY, domain TEXT, conf TEXT, ev TEXT, src TEXT)")
    return c


def base_key(r):
    return (r.get("page_id") or r.get("page_url") or r.get("page_name") or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=60)
    ap.add_argument("--cache-dir", default="")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    DIR = args.workdir
    SRC = os.path.join(DIR, "advertisers_clean.csv")
    DB = os.path.join(args.cache_dir or DIR, "domain_cache.sqlite")

    rows = list(csv.DictReader(open(SRC, encoding="utf-8", newline="")))
    pool = [r for r in rows if r.get("icp_verdict") == "Qualified" and not (r.get("domain") or "").strip()]
    conn = db(DB); cur = conn.cursor()
    have = {k for (k,) in cur.execute("SELECT k FROM d")}
    todo = [r for r in pool if base_key(r) not in have]
    if args.limit:
        todo = todo[:args.limit]
    print(f"Pool (Qualified, no domain): {len(pool)} | todo this run: {len(todo)} | blitz={'on' if _BLITZ.get('api_key') else 'off'}", flush=True)

    cost, done, blitz_hits, lock = [0.0], [0], [0], threading.Lock()

    def job(row):
        return row, *find_one(row)

    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for f in as_completed([ex.submit(job, r) for r in todo]):
                row, res, c = f.result()
                cur.execute("INSERT OR REPLACE INTO d VALUES(?,?,?,?,?)",
                            (base_key(row), res["domain"], res["conf"], res["ev"], res.get("src", "")))
                with lock:
                    cost[0] += c; done[0] += 1
                    if res.get("src") == "blitz":
                        blitz_hits[0] += 1
                    if done[0] % 25 == 0:
                        conn.commit(); print(f"  {done[0]}/{len(todo)}  ${cost[0]:.2f}  blitz={blitz_hits[0]}", flush=True)
        conn.commit()

    cache = {k: (d, cf, ev, src) for k, d, cf, ev, src in cur.execute("SELECT * FROM d")}
    evald = [r for r in pool if base_key(r) in cache]
    from collections import Counter
    conf_ct = Counter(cache[base_key(r)][1] for r in evald)
    usable = sum(1 for r in evald if cache[base_key(r)][0] and cache[base_key(r)][1] in ("high", "medium"))
    print(f"\n=== DOMAIN-FINDER ({len(evald)} evaluated) ===", flush=True)
    print(f"  high={conf_ct.get('high',0)} medium={conf_ct.get('medium',0)} low/none={conf_ct.get('low',0)}", flush=True)
    print(f"  usable (high/med w/ domain): {usable}  hit-rate {usable/max(len(evald),1)*100:.1f}%", flush=True)
    if done[0]:
        print(f"  cost this run: ${cost[0]:.4f}  per-row ${cost[0]/done[0]:.4f}  (blitz hits {blitz_hits[0]})", flush=True)

    if args.write:
        FIELDS = list(rows[0].keys())
        if "domain_confidence" not in FIELDS:
            FIELDS.append("domain_confidence")
        filled = 0
        for r in rows:
            r.setdefault("domain_confidence", "")
            hit = cache.get(base_key(r))
            if hit and r.get("icp_verdict") == "Qualified" and not (r.get("domain") or "").strip():
                dom, cf, ev, src = hit
                r["domain_confidence"] = cf
                if dom and cf in ("high", "medium"):
                    r["domain"] = dom; r["domain_source"] = src; filled += 1
        with open(SRC, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
        print(f"\n  wrote {filled} new domains back to {SRC}", flush=True)


if __name__ == "__main__":
    main()
