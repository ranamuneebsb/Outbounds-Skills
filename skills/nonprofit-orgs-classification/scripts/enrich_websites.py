"""
Step 6 - Enrich orgs with websites from IRS 990 e-file XML (multi-year waterfall).

Adds WEBSITE + WEBSITE_SOURCE (irs_990_<year>). Newest year wins. Hardened for
slow/flaky connections and for the IRS data's quirks.

Index : {base}/{year}/index_{year}.csv  (cols EIN, OBJECT_ID, XML_BATCH_ID)
Batch : {base}/{year}/{XML_BATCH_ID}.zip   XML files named {OBJECT_ID}_public.xml
Field : WebsiteAddressTxt

HARD-WON FIXES baked in (the value of this script):
  * 7-Zip extraction for EVERY batch - the largest IRS batches are Deflate64
    (compression method 9): Python zipfile lists them but CANNOT decompress
    ("compression method not supported"); bsdtar/libarchive can't open them at all.
    Fallback chain: 7-Zip -> Python zipfile (logs + skips Deflate64 if 7z absent).
  * 7z extracts with -r so members nested in a subfolder still match (some years
    nest XMLs); the file read also walks the extract dir recursively.
  * Uppercase-URL fallback: the IRS index sometimes stores XML_BATCH_ID lowercase
    (e.g. "..._04a") but the published ZIP is uppercase -> 404 -> retry uppercased.
  * Partial-download guard: verify Content-Length AND that the ZIP opens (catches
    silent truncation when no Content-Length header is sent).
  * EIN normalization on BOTH sides (strip, dashes, upper, string).
  * Checkpoint after EVERY batch; outer loop resumes across drops until done.
  * 404 on a year's index = that year not published yet -> skip, don't crash.

KNOWN, UNRECOVERABLE: some OBJECT_IDs the index assigns to a batch are physically
absent from that ZIP (IRS-side index/content mismatch) - nothing to do.

Example:
    python enrich_websites.py --input D:/Run/final.csv --output D:/Run/final_enriched.csv \
      --years 2026,2025,2024 --tmp-dir D:/Run/.tmp
"""
import argparse, csv, os, sys, time, json, subprocess, shutil, zipfile
import urllib.request, urllib.error
import xml.etree.ElementTree as ET
from collections import defaultdict

csv.field_size_limit(10**9)
PRINT_LOCK_BYTES = {"bytes": 0}


def log(m): print(m, flush=True)
def norm_ein(e): return (e or "").strip().replace("-", "").upper()


def normalize_url(url):
    if not url:
        return ""
    url = url.strip().replace("HTTPS: ", "https://").replace("HTTP: ", "http://")
    url = url.replace("https: //", "https://").replace("http: //", "http://").lower()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/").strip()


def find_7z(override):
    if override and os.path.exists(override):
        return override
    for p in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.exists(p):
            return p
    for name in ("7z", "7za"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def zip_ok(p):
    try:
        with zipfile.ZipFile(p) as zf:
            return len(zf.namelist()) > 0
    except Exception:
        return False


# ---------- checkpoint ----------
def load_cp(path):
    cp = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    cp.setdefault("results", {})
    cp.setdefault("completed_batches_by_year", {})
    cp.setdefault("completed_years", [])
    return cp


def save_cp(cp, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    json.dump(cp, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, path)


# ---------- download ----------
def download_zip(base, year, bid, tmp_dir, connect_to, read_to):
    dest = os.path.join(tmp_dir, f"{bid}.zip")
    backoffs = [5, 15, 45, 90, 180]
    attempt, tried_upper = 0, False
    url = f"{base}/{year}/{bid}.zip"
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=connect_to)
            try:
                resp.fp.raw._sock.settimeout(read_to)
            except Exception:
                pass
            total = int(resp.headers.get("Content-Length", 0) or 0)
            got = 0
            with open(dest, "wb") as f:
                while True:
                    c = resp.read(1 << 20)
                    if not c:
                        break
                    f.write(c); got += len(c)
            resp.close()
            if total and got != total:
                raise IOError(f"size mismatch {got}!={total}")
            if not zip_ok(dest):
                raise IOError("zip not openable (truncated)")
            PRINT_LOCK_BYTES["bytes"] += got
            return dest
        except urllib.error.HTTPError as e:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            if e.code == 404 and not tried_upper and bid != bid.upper():
                tried_upper = True
                log(f"  [casing] {bid} 404'd; retrying as {bid.upper()}")
                url = f"{base}/{year}/{bid.upper()}.zip"
                continue
            raise
        except Exception as e:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            if attempt >= len(backoffs):
                raise
            w = backoffs[attempt]; attempt += 1
            log(f"  [retry {attempt}/{len(backoffs)}] {bid}: {e} -> {w}s")
            time.sleep(w)


# ---------- parse ----------
def website_from_xml(fp_or_fileobj):
    root = ET.parse(fp_or_fileobj).getroot()
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "WebsiteAddressTxt" and el.text:
            return el.text.strip()
    return ""


def parse_batch(zip_path, ein_to_obj, sevenzip):
    obj_to_ein = {v: k for k, v in ein_to_obj.items()}
    results = {}
    if sevenzip:
        outdir = zip_path + "_x"
        shutil.rmtree(outdir, ignore_errors=True)
        os.makedirs(outdir, exist_ok=True)
        lst = zip_path + "_m.txt"
        with open(lst, "w", encoding="utf-8") as f:
            f.write("\n".join(f"{oid}_public.xml" for oid in obj_to_ein))
        subprocess.run([sevenzip, "e", zip_path, f"@{lst}", f"-o{outdir}", "-r", "-y", "-bso0", "-bsp0"],
                       capture_output=True, text=True)
        by_name = {}
        for rootd, _d, files in os.walk(outdir):
            for fn in files:
                by_name[fn] = os.path.join(rootd, fn)
        for oid, ein in obj_to_ein.items():
            fp = by_name.get(f"{oid}_public.xml")
            if not fp:
                continue
            try:
                site = website_from_xml(fp)
                results[ein] = normalize_url(site) if site else ""
            except Exception as e:
                log(f"    [xml-fail] {ein}: {e}")
                results[ein] = ""
        shutil.rmtree(outdir, ignore_errors=True)
        try:
            os.remove(lst)
        except OSError:
            pass
    else:
        # Python zipfile fallback - cannot decompress Deflate64 (logged & skipped)
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                oid = name.split("/")[-1].replace("_public.xml", "")
                if oid not in obj_to_ein:
                    continue
                ein = obj_to_ein[oid]
                try:
                    with zf.open(name) as xf:
                        site = website_from_xml(xf)
                    results[ein] = normalize_url(site) if site else ""
                except Exception as e:
                    log(f"    [xml-fail/deflate64?] {ein}: {e}")
                    results[ein] = ""
    return results


# ---------- index ----------
def scan_index(index_path, working):
    batch_map = defaultdict(dict)
    seen = set()
    with open(index_path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        cols = {c.upper(): c for c in reader.fieldnames}
        ein_c, bid_c, obj_c = cols.get("EIN"), cols.get("XML_BATCH_ID"), cols.get("OBJECT_ID")
        if not (ein_c and bid_c and obj_c):
            raise SystemExit(f"index missing EIN/XML_BATCH_ID/OBJECT_ID: {reader.fieldnames}")
        for r in reader:
            e = norm_ein(r.get(ein_c))
            if e in working and e not in seen:
                bid = (r.get(bid_c) or "").strip()
                oid = (r.get(obj_c) or "").strip()
                if bid and oid:
                    if bid.lower().endswith(".zip"):
                        bid = bid[:-4]
                    batch_map[bid][e] = oid
                    seen.add(e)
    return dict(batch_map)


def download_index(base, year, tmp_dir, connect_to, read_to):
    path = os.path.join(tmp_dir, f"index_{year}.csv")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        log(f"Using cached index_{year}.csv")
        return path
    os.makedirs(tmp_dir, exist_ok=True)
    url = f"{base}/{year}/index_{year}.csv"
    log(f"Downloading index_{year}.csv ...")
    try:
        download_to(url, path, connect_to, read_to)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log(f"  [warn] index_{year}.csv not published (404) - skipping {year}")
            return None
        raise
    return path


def download_to(url, dest, connect_to, read_to):
    backoffs = [5, 15, 45, 90, 180]; a = 0
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=connect_to)
            try:
                resp.fp.raw._sock.settimeout(read_to)
            except Exception:
                pass
            total = int(resp.headers.get("Content-Length", 0) or 0); got = 0
            with open(dest, "wb") as f:
                while True:
                    c = resp.read(1 << 20)
                    if not c:
                        break
                    f.write(c); got += len(c)
            resp.close()
            if total and got != total:
                raise IOError("size mismatch")
            return
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            if a >= len(backoffs):
                raise
            w = backoffs[a]; a += 1
            log(f"  [retry index {a}] {e} -> {w}s"); time.sleep(w)


# ---------- one pass ----------
def run_pass(base, year, src, working, index_path, cp, ck_path, sevenzip, tmp_dir, connect_to, read_to):
    log(f"\n{'='*56}\nPASS {year}  (working set: {len(working):,})\n{'='*56}")
    bm = scan_index(index_path, working)
    matched = sum(len(v) for v in bm.values())
    log(f"  matched {matched:,}/{len(working):,} across {len(bm)} batch ZIP(s)")
    done = set(cp["completed_batches_by_year"].get(year, []))
    todo = [b for b in sorted(bm) if b not in done]
    if not todo:
        log("  all batches already done"); return True
    log(f"  {len(todo)} batch ZIP(s) to fetch")

    def fetch_parse(bid):
        zp = download_zip(base, year, bid, tmp_dir, connect_to, read_to)
        res = parse_batch(zp, bm[bid], sevenzip)
        try:
            os.remove(zp)
        except OSError:
            pass
        return res

    failed = []
    for i, bid in enumerate(todo, 1):
        try:
            res = fetch_parse(bid)
        except Exception as e:
            log(f"  [batch-fail] {bid}: {e} - deferring"); failed.append(bid); continue
        hits = sum(1 for u in res.values() if u)
        for ein, url in res.items():
            if url:
                cp["results"][ein] = {"website": url, "source": src}
        done.add(bid)
        cp["completed_batches_by_year"][year] = sorted(done)
        save_cp(cp, ck_path)
        th = sum(1 for v in cp["results"].values() if v.get("website"))
        log(f"  [{i}/{len(todo)}] {bid}: {hits}/{len(bm[bid])} sites | total {th:,} | {PRINT_LOCK_BYTES['bytes']/1024**3:.2f} GB")
    if failed:
        log(f"  retry sweep: {len(failed)} deferred")
        still = []
        for bid in failed:
            try:
                res = fetch_parse(bid)
                for ein, url in res.items():
                    if url:
                        cp["results"][ein] = {"website": url, "source": src}
                done.add(bid); cp["completed_batches_by_year"][year] = sorted(done); save_cp(cp, ck_path)
            except Exception as e:
                log(f"  [batch-fail] {bid}: still failing ({e})"); still.append(bid)
        if still:
            log(f"  {len(still)} still unfetched: {still}")
    return all(b in done for b in bm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--years", default="2026,2025,2024")
    ap.add_argument("--base-url", default="https://apps.irs.gov/pub/epostcard/990/xml")
    ap.add_argument("--tmp-dir", required=True)
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--connect-timeout", type=float, default=60)
    ap.add_argument("--read-timeout", type=float, default=600)
    ap.add_argument("--7z", dest="sevenzip", default=None, help="path to 7z.exe (else auto-detect)")
    ap.add_argument("--wait", type=int, default=120, help="seconds between outer resume rounds")
    args = ap.parse_args()

    years = [y.strip() for y in args.years.split(",") if y.strip()]
    ck = args.checkpoint or os.path.join(args.tmp_dir, "enrich_websites_checkpoint.json")
    sevenzip = find_7z(args.sevenzip)
    log(f"7-Zip: {sevenzip or 'NOT FOUND (falling back to Python zipfile - Deflate64 batches will be skipped)'}")

    with open(args.input, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames)
        rows = list(reader)
    our = {norm_ein(r.get("EIN")) for r in rows}; our.discard("")
    log(f"Loaded {len(rows):,} rows ({len(our):,} unique EINs)")

    cp = load_cp(ck)
    src_of = {y: f"irs_990_{y}" for y in years}
    index_paths = {y: download_index(args.base_url, y, args.tmp_dir, args.connect_timeout, args.read_timeout) for y in years}

    while True:
        all_done = True
        for year in years:
            if year in cp["completed_years"]:
                continue
            if index_paths.get(year) is None:
                cp["completed_years"].append(year); save_cp(cp, ck); continue
            hits = {e for e, v in cp["results"].items() if v.get("website")}
            working = our - hits
            try:
                complete = run_pass(args.base_url, year, src_of[year], working, index_paths[year],
                                    cp, ck, sevenzip, args.tmp_dir, args.connect_timeout, args.read_timeout)
            except Exception as e:
                log(f"  [pass-error] {year}: {e}"); complete = False
            if complete:
                cp["completed_years"].append(year); save_cp(cp, ck)
            else:
                all_done = False; break
        if all_done:
            break
        log(f"\n[round wait {args.wait}s, then resume]"); time.sleep(args.wait)

    out_fields = fields + [c for c in ("WEBSITE", "WEBSITE_SOURCE") if c not in fields]
    res = cp["results"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields); w.writeheader()
        for r in rows:
            rr = res.get(norm_ein(r.get("EIN")))
            r["WEBSITE"] = rr["website"] if rr else r.get("WEBSITE", "")
            r["WEBSITE_SOURCE"] = rr["source"] if rr and rr.get("website") else r.get("WEBSITE_SOURCE", "")
            w.writerow(r)

    n = len(rows)
    enriched = sum(1 for r in rows if (r.get("WEBSITE_SOURCE") or "").strip())
    log("\n" + "=" * 56)
    log("IRS 990 ENRICHMENT - SUMMARY")
    log(f"Enriched: {enriched:,}/{n:,} ({enriched/n*100:.1f}%) | {PRINT_LOCK_BYTES['bytes']/1024**3:.2f} GB | Output: {args.output}")
    log("=" * 56)


if __name__ == "__main__":
    main()
