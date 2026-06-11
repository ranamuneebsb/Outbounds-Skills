SKILL NAME: Ads Library Pipeline
VERSION: 1.0
AUTHOR: OlympoLabs

================================================================
STOP — WAIT FOR THE USER'S INPUTS (read this first)
================================================================
This skill is MECHANICS ONLY. Nothing campaign-specific is baked in. Before
running anything, WAIT for the user to provide, FOR THIS run:

  1. KEYWORDS               — the parent keyword list (a file path or pasted text)
  2. ICP / CLASSIFICATION   — the full classification prompt (qualify rules,
     PROMPT                    disqualify rules, clarifications, niche list)
  3. PERSONALIZATION PROMPT — the personalization prompt (format rules, framing,
                              examples)
  4. COUNTRIES, THRESHOLDS, FILE LOCATIONS — per run

NEVER invent, reuse, or assume keywords / ICP / personalization from a previous
campaign or from these templates. If ANY of the three is missing, ASK for it and
stop. The templates/ files are FORMAT REFERENCES ONLY — the pipeline never reads
them and you must not auto-use them as defaults.

The prompt-hash cache invalidation enforces this at the data level: a new ICP or
personalization prompt produces a new prompt-hash, so classifications/lines are
regenerated fresh and no stale verdicts ever leak between campaigns.

================================================================
PURPOSE
================================================================
Apify Meta Ad Library scrape -> a classified, domain-verified, personalized B2B
outreach list. Given a keyword list + an ICP prompt + a personalization prompt,
it scrapes every keyword from the Meta Ad Library, collapses ads to one row per
advertiser, classifies each against the ICP from ad copy alone, rescues the
unclear ones with web search, finds official domains for the qualified rows, and
writes a ready-to-send personalized line per company. Built from the Meta Ads
Diversification campaign.

The scripts are already implemented under scripts/. Claude Code RUNS them with
the user's per-run inputs as arguments. It does NOT hardcode anything.

================================================================
INPUTS PER CAMPAIGN (all user-provided, every run)
================================================================
  --keywords PATH             one parent keyword per line
  --icp-prompt PATH           the classification prompt (used by classify_icp.py
                              AND pass_online.py — same file)
  --personalize-prompt PATH   the personalization prompt (used by personalize_ads.py)
  --countries US,CA,GB        comma-separated ISO codes
  --workdir PATH              campaign folder; ALL intermediate + output files
                              live here
  --cache-dir PATH            optional shared cache for cross-campaign reuse
  --score-threshold           Qualified is icp_score >= 7 (the ICP prompt owns
                              the rubric)

================================================================
API KEYS — FILES ONLY, NEVER HARDCODED, NEVER FROM ENV
================================================================
  ~/.olympolabs/openrouter.json   {"api_key": "sk-or-v1-..."}   (all LLM calls)
  ~/.olympolabs/apify.json        {"api_key": "apify_api_..."}  (scraping)
  ~/.olympolabs/blitz.json        {"api_key": "...", "base_url": "..."}  (domain
                                  lookup; OPTIONAL — domain finder degrades to the
                                  online-LLM fallback if absent)
No key is ever read from the environment or written into a script.

================================================================
MODEL CONFIG (constants at the top of each script; override via flags)
================================================================
  CLASSIFY_MODEL     = google/gemini-2.5-flash-lite     (copy-only classify)
  CLASSIFY_FALLBACK  = openai/gpt-4o-mini               (per-row hard-error fallback)
  SEARCH_MODEL       = openai/gpt-4o-mini:online        (rescue + domain finder web search)
  PERSONALIZE_MODEL  = anthropic/claude-haiku-4.5       (personalization, one pass)

================================================================
THE 11 STAGES (with model assignments)
================================================================
 1. SCRAPE        apify_scrape.py — fires ALL keyword runs concurrently via the
                  apify-client API; pulls results as JSON datasets (NOT CSV, to
                  avoid the 4,058-column flatten); extracts only needed fields ->
                  ads_slim.csv. (merge_csvs.py is the legacy fallback for manual
                  CSV drops.)                                       [Apify]
 2. SLIM          (done inside stage 1 — ads_slim.csv is the slim per-ad file)
 3. DEDUPE        dedupe_advertisers.py — collapse per-ad rows to one per
                  advertiser, keyed FB page URL -> page_id -> page_name. Also does
                  the FREE ad-link domain extraction (clean/funnel/social).   [—]
 4. CLEANUP       cleanup_pass.py — reclassify aggregator/tracker hosts to blank,
                  root-normalize real domains, drop consumer categories + content
                  farms.                                                       [—]
 5. REBUILD COPY  rebuild_adcopy.py — backfill the 5 fullest distinct ad bodies +
                  3 titles per advertiser into ad_copy_full / ad_titles.       [—]
 6. CLASSIFY      classify_icp.py — score each advertiser against the ICP from ad
                  copy. PILOT 100 first (==GATE 1==), then full --write.
                                                  [gemini-2.5-flash-lite / gpt-4o-mini]
 7. RESCUE A+B    pass_online.py --pass A and --pass B, run IN PARALLEL. A re-judges
                  "Review" rows; B re-judges no-copy "error" rows. Both web-search
                  the company, resolve the verdict, and return any domain found.
                                                            [gpt-4o-mini:online]
 8. DOMAIN FIND   domain_finder.py — for Qualified rows still missing a domain:
                  name-clean -> Blitz -> gpt-4o-mini:online fallback. Writes only
                  high/medium-confidence, non-blocklisted domains.
                                                       [Blitz / gpt-4o-mini:online]
 9. FINALIZE      finalize.py — backfill domain from web-searched domain_found,
                  export Qualified -> qualified_advertisers.csv.               [—]
10. PERSONALIZE   personalize_ads.py — one personalization line per Qualified
                  advertiser, ONE pass that emits the final ready-to-paste format
                  directly (the old reformat step is merged in). PILOT 20
                  (==GATE 3==), then full --write.            [claude-haiku-4.5]
11. EXPORT        snap_and_export.py — optional niche-alias snapping (user map) +
                  final qualified_advertisers_final.csv.                       [—]

Everything is SQLite-cached and idempotent; --resume re-runs the chain and each
stage skips what's already done.

================================================================
THE 3 GATES (and why they exist)
================================================================
GATE 1 — after the 100-row CLASSIFY PILOT, before the full classify.
  WHY: the ICP prompt drives every downstream cost. You look at the pilot's
  verdict/niche distribution and sample rows and confirm the classifier is
  calibrated BEFORE spending on the whole list. If it's off, edit the ICP prompt
  and re-run — the prompt-hash busts the cache so nothing stale survives.

GATE 2 — after classify + rescue, before the paid DOMAIN FINDER.
  WHY: domain finding is the most expensive pass (online web search). The gate
  prints how many Qualified rows still lack a domain and the rough cost ceiling so
  you approve the spend deliberately, not by accident.

GATE 3 — after the 20-row PERSONALIZATION PILOT, before the full personalization
  write and any optional Blitz enrichment.
  WHY: personalization writes the line that actually goes in the email. You eyeball
  20 rows for framing/format, and you gate the Blitz-credit enrichment, before
  committing the full run.

On a non-interactive terminal (or with --yes) the gates auto-proceed.

================================================================
HARD-LEARNED RULES (verbatim — do not skip these)
================================================================
  * Merge by column NAME never position.
  * Dedupe by FB page URL BEFORE anything paid (4.7x collapse).
  * Classify from ad copy BEFORE paying for domains/scraping.
  * Pilot 100 rows before every full run; prompt-hash cache invalidation.
  * Gemini check: compare real billed cost (usage.include) vs gpt-4o-mini in the
    pilot - hidden reasoning tokens can make it pricier; fall back if so.
  * Error path on every LLM call - do not guess.
  * Never enrich an unverified domain.
  * Trackers/aggregators are not domains (doubleclick, whatsapp, linktree...).
  * Re-verify ad status the week of delivery (the angle dies otherwise).

================================================================
COST BENCHMARKS (from the real run)
================================================================
  classify        ~$0.0002 / row
  search passes   ~$0.0089 / row   (rescue A/B + domain finder online fallback)
  personalize     ~$0.0006 / row
  full 68k-ad campaign  ≈  $25-40 total

Pilot every full run first; the Gemini-vs-gpt-4o-mini cost compare in the
classify pilot can move the classify number either way.

================================================================
COMMAND SYNTAX
================================================================
Run the whole thing through the orchestrator:

    python scripts/pipeline.py \
      --workdir   D:/campaigns/acme \
      --keywords  D:/campaigns/acme/keywords.txt \
      --icp-prompt        D:/campaigns/acme/icp.md \
      --personalize-prompt D:/campaigns/acme/personalize.md \
      --countries US,CA \
      --max-ads 500

  --resume          re-run; skip finished work (scrape skipped if ads_slim exists)
  --yes             auto-proceed past all 3 gates (unattended)
  --no-scrape       skip Apify; use a manual CSV drop (run merge_csvs.py first)
  --cache-dir PATH  shared cache for cross-campaign reuse
  --classify-model M  override the classify primary (e.g. after a Gemini cost check)
  --enrich-script PATH  optional Blitz-enrichment script run after GATE 3

Or run any stage standalone (each takes --workdir and reads/writes canonical
filenames in it):

    python scripts/classify_icp.py --workdir D:/c/acme --icp-prompt D:/c/acme/icp.md --limit 100 --cost-compare
    python scripts/classify_icp.py --workdir D:/c/acme --icp-prompt D:/c/acme/icp.md --write
    python scripts/pass_online.py  --workdir D:/c/acme --icp-prompt D:/c/acme/icp.md --pass both --write
    python scripts/domain_finder.py --workdir D:/c/acme --write
    python scripts/personalize_ads.py --workdir D:/c/acme --personalize-prompt D:/c/acme/pers.md --limit 20

================================================================
INTERMEDIATE + OUTPUT FILES (all in --workdir)
================================================================
  ads_slim.csv                  slim per-ad rows from Apify JSON (or merged_all.csv legacy)
  advertisers_deduped.csv       one row per advertiser + free ad-link domains
  advertisers_clean.csv         post-cleanup working file (classify/rescue/domain write here)
  advertisers_excluded.csv      category + content-farm drops, with reasons
  qualified_advertisers.csv     Qualified only (after finalize; gets ads_promote)
  qualified_advertisers_final.csv  the deliverable (after snap & export)
  *.sqlite                      icp / online / domain / personalize caches

================================================================
NOTES FOR CLAUDE CODE
================================================================
  - Confirm you have all THREE user inputs (keywords, ICP prompt, personalization
    prompt) before starting. If any is missing, ASK — do not proceed.
  - Run in the FOREGROUND so the per-stage reports and gate prompts are visible.
  - Use forward slashes in paths on Windows.
  - Keys come from ~/.olympolabs/*.json only. Never hardcode, never read env.
  - apify_scrape.py auto-installs apify-client if missing. The default actor and
    its input may need adjusting per actor (--actor / --actor-input).
  - On Windows set PYTHONIOENCODING=utf-8 to avoid console encoding errors on ad copy.
