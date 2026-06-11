#!/usr/bin/env python3
"""pipeline.py — orchestrate the full ads-library-pipeline with 3 human gates.

NOTHING campaign-specific is baked in. You MUST pass, every run:
  --keywords PATH             user-provided keyword file
  --icp-prompt PATH           user-provided ICP/classification prompt
  --personalize-prompt PATH   user-provided personalization prompt

Chain:
  scrape(apify -> ads_slim) -> dedupe -> cleanup -> rebuild copy
  -> classify PILOT(100)  ==GATE 1: calibration==  -> classify FULL
  -> rescue A + B (IN PARALLEL)  ==GATE 2: cost check==
  -> domain finder (name-clean -> Blitz -> 4o-mini:online)
  -> finalize -> personalize  ==GATE 3: before Blitz enrichment==
  -> snap & export -> [optional enrichment]

Every stage is SQLite-cached and idempotent; --resume re-runs the chain and each
stage skips what's already done (and skips the scrape if ads_slim.csv exists).
Point --cache-dir at a shared folder to reuse classify/personalize results across
campaigns (cache keys are page_id + prompt-hash, so a new prompt = fresh results).

    python pipeline.py --workdir D:/c/acme \
        --keywords D:/c/acme/keywords.txt \
        --icp-prompt D:/c/acme/icp.md \
        --personalize-prompt D:/c/acme/pers.md \
        --countries US,CA
"""
import os, sys, csv, time, argparse, subprocess

csv.field_size_limit(10**9)
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(script, *a):
    print(f"\n{'='*70}\n>>> {script} {' '.join(str(x) for x in a)}\n{'='*70}", flush=True)
    subprocess.run([PY, os.path.join(SCRIPTS, script), *map(str, a)], cwd=SCRIPTS, check=True)


def gate(n, title, lines, auto):
    print(f"\n{'#'*70}\n#  GATE {n}: {title}\n{'#'*70}")
    for l in lines:
        print("#  " + l)
    print("#" + "-" * 69)
    if auto:
        print("#  --yes set -> auto-proceeding.\n")
        return
    if not sys.stdin.isatty():
        print("#  non-interactive stdin -> auto-proceeding.\n")
        return
    ans = input(f"#  Proceed past GATE {n}? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        sys.exit(f"Stopped at GATE {n} by user.")


def count_qual_no_domain(workdir):
    src = os.path.join(workdir, "advertisers_clean.csv")
    q = qnd = 0
    if not os.path.exists(src):
        return 0, 0
    for r in csv.DictReader(open(src, encoding="utf-8", newline="")):
        if r.get("icp_verdict") == "Qualified":
            q += 1
            if not (r.get("domain") or "").strip():
                qnd += 1
    return q, qnd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--keywords", default="", help="USER-PROVIDED keyword file (omit + --no-scrape for manual CSV drops)")
    ap.add_argument("--icp-prompt", dest="icp", required=True, help="USER-PROVIDED ICP/classification prompt file")
    ap.add_argument("--personalize-prompt", dest="pers", required=True, help="USER-PROVIDED personalization prompt file")
    ap.add_argument("--countries", default="US")
    ap.add_argument("--max-ads", type=int, default=500)
    ap.add_argument("--cache-dir", default="", help="Shared cache dir for cross-campaign reuse (default workdir)")
    ap.add_argument("--classify-model", default="")
    ap.add_argument("--pilot", type=int, default=100)
    ap.add_argument("--no-scrape", action="store_true", help="Skip Apify; expect ads_slim.csv or merged_all.csv in workdir")
    ap.add_argument("--enrich-script", default="", help="Optional Blitz-enrichment script run after GATE 3")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--yes", action="store_true", help="Auto-proceed past all gates (unattended)")
    args = ap.parse_args()

    wd = args.workdir
    cache = ["--cache-dir", args.cache_dir] if args.cache_dir else []
    cmodel = ["--model", args.classify_model] if args.classify_model else []
    t0 = time.time()

    # 1) SCRAPE -> ads_slim.csv
    if not args.no_scrape:
        if not args.keywords:
            sys.exit("Provide --keywords (or use --no-scrape with a manual CSV drop).")
        sc = ["apify_scrape.py", "--workdir", wd, "--keywords", args.keywords,
              "--countries", args.countries, "--max-ads", args.max_ads]
        if args.resume:
            sc.append("--resume")
        run(*sc)
    elif not (os.path.exists(os.path.join(wd, "ads_slim.csv")) or os.path.exists(os.path.join(wd, "merged_all.csv"))):
        sys.exit("--no-scrape but no ads_slim.csv / merged_all.csv in workdir. "
                 "Drop Apify CSVs and run merge_csvs.py first, or remove --no-scrape.")

    # 2) DEDUPE (folds free ad-link domain extraction) -> 3) CLEANUP -> 4) REBUILD COPY
    run("dedupe_advertisers.py", "--workdir", wd)
    run("cleanup_pass.py", "--workdir", wd)
    run("rebuild_adcopy.py", "--workdir", wd)

    # 5) CLASSIFY PILOT -> GATE 1
    run("classify_icp.py", "--workdir", wd, "--icp-prompt", args.icp, "--limit", args.pilot, *cmodel)
    gate(1, "Classifier calibration",
         ["Review the pilot verdict/niche distribution + sample rows above.",
          "Qualified rate sane? Niches correct? Errors explainable?",
          "If miscalibrated: edit the ICP prompt and re-run (prompt-hash busts the cache)."],
         args.yes)

    # 6) CLASSIFY FULL
    run("classify_icp.py", "--workdir", wd, "--icp-prompt", args.icp, "--write", *cmodel)

    # 7) RESCUE A + B IN PARALLEL
    print("\n>>> rescue passes A + B (parallel)\n", flush=True)
    procs = [subprocess.Popen([PY, os.path.join(SCRIPTS, "pass_online.py"),
                               "--workdir", wd, "--icp-prompt", args.icp, "--pass", p, "--write", *cache], cwd=SCRIPTS)
             for p in ("A", "B")]
    rc = [p.wait() for p in procs]
    if any(rc):
        sys.exit(f"Rescue pass failed (return codes {rc}).")

    # GATE 2: cost check before paid domain finding
    q, qnd = count_qual_no_domain(wd)
    gate(2, "Domain-finder cost check",
         [f"Qualified advertisers: {q}", f"Qualified WITHOUT a domain: {qnd}",
          f"Domain finder will web-search those {qnd} rows (~$0.0089/row online fallback;",
          f"Blitz hits are far cheaper). Rough ceiling ~${qnd*0.0089:.2f}."],
         args.yes)

    # 8) DOMAIN FINDER
    run("domain_finder.py", "--workdir", wd, "--write", *cache)

    # 9) FINALIZE -> 10) PERSONALIZE (pilot then full)
    run("finalize.py", "--workdir", wd)
    run("personalize_ads.py", "--workdir", wd, "--personalize-prompt", args.pers, "--limit", 20, *cache)
    gate(3, "Before personalization full-write + Blitz enrichment",
         ["Review the 20-row personalization pilot above.",
          "If the framing/format is right, proceed to full personalization write,",
          "then optional Blitz enrichment (only if --enrich-script was provided)."],
         args.yes)
    run("personalize_ads.py", "--workdir", wd, "--personalize-prompt", args.pers, "--write", *cache)

    # 11) SNAP & EXPORT
    run("snap_and_export.py", "--workdir", wd)

    # optional enrichment (mechanics-only skill bundles no campaign enricher)
    if args.enrich_script:
        run(args.enrich_script, "--workdir", wd, *cache)
    else:
        print("\n(no --enrich-script provided -> skipping Blitz enrichment stage)")

    print(f"\nPipeline complete in {(time.time()-t0)/60:.1f} min.")
    print(f"Deliverable: {os.path.join(wd, 'qualified_advertisers_final.csv')}")


if __name__ == "__main__":
    main()
