"""
Step 8 (optional) - LLM web-search fallback for still-blank websites.

Fills WEBSITE for blank rows via an OpenRouter :online model (web search), with
STRICT correctness gating. Adds gemini_confidence + gemini_reason audit columns.
Key: ~/.olympolabs/openrouter.json

Default model: google/gemini-2.5-flash-lite:online  (cheap; :online = web search).
NOTE: flash-lite has stalled under high concurrency - default workers=5, 40s
timeout, and timed-out rows are DEFERRED to an end-of-run retry sweep, not blocked.

Strict gate (reject -> leave blank) if ANY:
  1. website NONE/empty
  2. host in blocklist (directories/socials/.gov/wikipedia/aggregators/parked sellers)
  3. confidence != "high"
  4. NAME-vs-DOMAIN: no name token (>=4 chars, minus inc/foundation/the/of/for/
     center/association/society/services) appears in the domain, UNLESS the name's
     acronym appears (legit kwrcc.org-style; corporate suffixes excluded from the
     acronym so jys.org for "Juneau Youth Services Inc" still matches).
JSON parsed defensively (strips ``` fences, extracts the {...} object).

Pilot:  python enrich_llm.py --input X --output Y --pilot 20
Full :  python enrich_llm.py --input X --output Y --full
"""
import argparse, csv, os, json, time, re
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

csv.field_size_limit(10**9)
URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = (
    "You find the official website of a US nonprofit. Return ONLY the org's own "
    "official domain. NEVER return: directory/aggregator sites (guidestar, "
    "charitynavigator, causeiq, propublica, idealist, facebook, linkedin, instagram, "
    "x/twitter, yelp, mapquest, .gov pages, wikipedia), a parent or unrelated org, or "
    "a guess. If not confident it is THIS org's own site, return NONE."
)
USER = ("Nonprofit: {name}\nCity/State: {city}, {state}\nRespond in JSON only:\n"
        "{{\"website\":\"<url or NONE>\",\"confidence\":\"high|low\",\"reason\":\"<5 words>\"}}")

BLOCK = ["guidestar", "charitynavigator", "causeiq", "propublica", "idealist",
         "facebook.", "fb.com", "linkedin.", "instagram.", "twitter.", "x.com",
         "yelp.", "mapquest", "wikipedia.", "hugedomains", "afternic", "domainmarket",
         "sedo.", "dan.com", "godaddy", "wix.com", "wordpress.com", "blogspot",
         "youtube.", "google.com/maps"]
TOKEN_STOP = {"inc", "incorporated", "corp", "foundation", "the", "of", "for",
              "center", "association", "society", "services"}
ACRO_STOP = {"THE", "OF", "AND", "FOR", "A", "INC", "INCORPORATED", "CORP",
             "CORPORATION", "LLC", "LTD", "CO"}


def norm_ein(e): return (e or "").strip().replace("-", "").upper()


def normalize_url(url):
    url = (url or "").strip().lower()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/").strip()


def host_of(url):
    h = re.sub(r"^https?://", "", (url or "").lower()).split("/")[0]
    return h[4:] if h.startswith("www.") else h


def extract_json(text):
    if not text:
        return None
    t = re.sub(r"```$", "", re.sub(r"^```(?:json)?", "", text.strip()).strip()).strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j < i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def name_domain_match(name, host):
    words = re.findall(r"[A-Za-z]+", name.upper())
    flat = re.sub(r"[^a-z0-9]", "", host.lower())
    for w in words:
        if len(w) >= 4 and w.lower() not in TOKEN_STOP and w.lower() in flat:
            return True
    acro = "".join(w[0] for w in words if w not in ACRO_STOP).lower()
    return len(acro) >= 2 and acro in flat


def validate(name, obj):
    if not obj or not isinstance(obj, dict):
        return False, "", "malformed"
    site = (obj.get("website") or "").strip()
    if not site or site.upper() == "NONE":
        return False, "", "none"
    host = host_of(site)
    if not host:
        return False, "", "none"
    if host.endswith(".gov") or any(b in host for b in BLOCK):
        return False, "", "blocklist"
    if (obj.get("confidence") or "").strip().lower() != "high":
        return False, "", "low_conf"
    if not name_domain_match(name, host):
        return False, "", "name_mismatch"
    return True, normalize_url(site), "accepted"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--key-file", default=os.path.expanduser("~/.olympolabs/openrouter.json"))
    ap.add_argument("--model", default="google/gemini-2.5-flash-lite:online")
    ap.add_argument("--source-tag", default="gemini_flash_lite")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=40)
    ap.add_argument("--pilot", type=int, default=0)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    api_key = json.load(open(args.key_file, encoding="utf-8"))["api_key"]
    ck = args.checkpoint or (os.path.splitext(args.output)[0] + ".llm_ckpt.json")

    def ask(name, city, state):
        payload = json.dumps({
            "model": args.model,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": USER.format(name=name, city=city, state=state)}],
            "temperature": 0, "max_tokens": 200,
        }).encode("utf-8")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            req = urllib.request.Request(URL, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                raw = json.loads(resp.read())["choices"][0]["message"]["content"]
            return extract_json(raw), "ok"
        except (urllib.error.URLError, TimeoutError) as e:
            if "timed out" in str(e).lower() or isinstance(e, TimeoutError):
                return None, "timeout"
            return None, "error"
        except Exception:
            return None, "error"

    def process(row):
        name = row.get("NAME", "")
        obj, status = ask(name, row.get("CITY", ""), row.get("STATE", ""))
        if status == "timeout":
            return {"ein": norm_ein(row.get("EIN")), "status": "timeout"}
        accepted, url, reason = validate(name, obj)
        return {"ein": norm_ein(row.get("EIN")), "status": "ok", "name": name,
                "accepted": accepted, "url": url, "reject": reason,
                "raw": (obj.get("website") if obj else ""),
                "conf": (obj.get("confidence") if obj else ""),
                "reason_txt": (obj.get("reason") if obj else ""), "malformed": obj is None}

    with open(args.input, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.DictReader(f)); fields = list(rows[0].keys())
    blanks = [r for r in rows if not (r.get("WEBSITE") or "").strip()]
    print(f"website-blank: {len(blanks):,} | model={args.model} | workers={args.workers}", flush=True)

    if args.pilot and not args.full:
        sample = blanks[:args.pilot]
        results = [None] * len(sample)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process, r): i for i, r in enumerate(sample)}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
        acc = sum(1 for r in results if r.get("accepted"))
        mal = sum(1 for r in results if r.get("malformed"))
        to = sum(1 for r in results if r.get("status") == "timeout")
        rej = Counter(r.get("reject") for r in results if r.get("status") == "ok" and not r.get("accepted"))
        for r in results[:10]:
            if r.get("status") == "timeout":
                print(f"  [TIMEOUT] {r['ein']}"); continue
            v = "ACCEPT" if r["accepted"] else f"REJECT({r['reject']})"
            print(f"  {r['name']} -> {r.get('raw') or '(none)'} [{v}] conf={r.get('conf')} reason={r.get('reason_txt')}")
        print(f"\nPILOT accepted {acc}/{len(sample)} | rejects {dict(rej)} | malformed {mal} | timeouts {to}")
        return

    done = json.load(open(ck, encoding="utf-8")) if os.path.exists(ck) else {}
    todo = [r for r in blanks if norm_ein(r.get("EIN")) not in done]
    deferred = []
    for s in range(0, len(todo), 100):
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for res in ex.map(process, todo[s:s + 100]):
                if res.get("status") == "timeout":
                    deferred.append(res["ein"]); continue
                done[res["ein"]] = {"url": res["url"] if res["accepted"] else "",
                                    "conf": res.get("conf") or "", "reason": res.get("reason_txt") or "",
                                    "reject": res["reject"]}
        json.dump(done, open(ck, "w", encoding="utf-8"))
        acc = sum(1 for v in done.values() if v.get("url"))
        print(f"  attempted {len(done):,}/{len(blanks):,} | accepted {acc:,} | deferred {len(deferred)}", flush=True)

    ein2row = {norm_ein(r.get("EIN")): r for r in todo}
    still_to = 0
    if deferred:
        print(f"Retry sweep: {len(deferred)} deferred", flush=True)
        for ein in deferred:
            res = process(ein2row[ein])
            if res.get("status") == "timeout":
                still_to += 1; done[ein] = {"url": "", "conf": "", "reason": "", "reject": "timeout"}
            else:
                done[ein] = {"url": res["url"] if res["accepted"] else "",
                             "conf": res.get("conf") or "", "reason": res.get("reason_txt") or "", "reject": res["reject"]}
            json.dump(done, open(ck, "w", encoding="utf-8"))

    out_fields = fields + [c for c in ("gemini_confidence", "gemini_reason") if c not in fields]
    rej = Counter(); accepted = 0
    for r in rows:
        r.setdefault("gemini_confidence", ""); r.setdefault("gemini_reason", "")
        v = done.get(norm_ein(r.get("EIN")))
        if v and v.get("url") and not (r.get("WEBSITE") or "").strip():
            r["WEBSITE"] = v["url"]; r["WEBSITE_SOURCE"] = args.source_tag
            r["gemini_confidence"] = v.get("conf", ""); r["gemini_reason"] = v.get("reason", "")
            accepted += 1
        elif v and not v.get("url"):
            rej[v.get("reject", "?")] += 1
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields); w.writeheader(); w.writerows(rows)

    total = len(rows); enriched = sum(1 for r in rows if (r.get("WEBSITE_SOURCE") or "").strip())
    print("\n" + "=" * 56)
    print(f"LLM FALLBACK ({args.model}): attempted {len(blanks):,} | accepted {accepted:,}")
    print(f"Rejected by reason: {dict(rej)} | still timed-out: {still_to}")
    print(f"Coverage now {enriched/total*100:.1f}% ({enriched:,}/{total:,}) | Output {args.output}")
    print("=" * 56)


if __name__ == "__main__":
    main()
