SKILL NAME: Nonprofit Orgs Classification
VERSION: 1.0
AUTHOR: OlympoLabs

================================================================
STOP — WAIT FOR THE USER'S SPEC (read this first)
================================================================
This skill is MECHANICS ONLY. It encodes the IRS-990 list-building PIPELINE; it does
NOT know any client's targeting. Before running anything, WAIT for the user to
provide, FOR THIS run:

  1. ICP FILTERS        — subsection, deductibility, foundation codes, revenue floor
                          (and optional ceiling), and which columns to keep
  2. NOBLE-CAUSE RULES  — a noble_causes config JSON (good/drop letters, code3
                          overrides both directions, blank-NTEE handling).
                          templates/noble-causes-example.json is the Design Desk
                          example — REPLACE it per client, do not reuse as default.
  3. EMAILED DEDUPE FILE — path to the previously-emailed EIN list (single column)
  4. FILE LOCATIONS / YEARS — working dir, enrichment years (default 2026,2025,2024)

NEVER assume Design Desk's values (501(c)(3) + deductible + foundation 10/11/15 +
$800k + its noble-cause rules). Those are an EXAMPLE. If the ICP filters or the
noble_causes JSON are missing, ASK and stop. The templates/ file is a FORMAT
REFERENCE — the pipeline only reads the config path you pass it.

NEVER commit client data, EINs, the dedupe file, or API keys. Keys are read from
~/.olympolabs/*.json only (never hardcoded, never from env).

================================================================
PURPOSE
================================================================
IRS Exempt-Organizations Business Master File -> a filtered, noble-cause-classified,
de-emailed, website-enriched B2B nonprofit list. Download every state's EO BMF,
merge+dedupe by EIN, filter to the client's ICP, classify by NTEE "noble cause"
rules, drop already-emailed orgs, then enrich websites via the IRS 990 e-file XML
(multi-year waterfall) with optional Blitz + LLM web-search fallbacks. Generalized
from the Design Desk build (34,857 orgs, 97.3% website coverage).

The scripts are implemented under scripts/. Claude Code RUNS them with the user's
per-run spec as arguments. It does NOT hardcode anything.

================================================================
PIPELINE ORDER
================================================================
  download_states.py   1. Download all 50 states + DC EO BMF CSVs (resume-safe)
  merge_states.py      2. Merge -> one file, dedupe by EIN, add source_state
  filter_icp.py        3. Filter to the client ICP (configurable conditions)
  classify_ntee.py     4. Classify good_feel / not_good_feel / no_ntee (config JSON)
  dedupe_emailed.py    5. Drop EINs already in the emailed file -> fresh list
  enrich_websites.py   6. IRS 990 multi-year waterfall (newest year wins)   [free]
  enrich_blitz.py      7. OPTIONAL BlitzAPI fallback for still-blank rows    [credits]
  enrich_llm.py        8. OPTIONAL LLM :online web-search fallback           [LLM $]

Stages 4 keeps no_ntee rows in their own file (never dropped). Stage 6 alone reaches
~90% website coverage; 6+7+8 reaches ~97%. Run each stage in the FOREGROUND so the
per-stage report is visible. On Windows set PYTHONIOENCODING=utf-8.

================================================================
API KEYS — FILES ONLY
================================================================
  ~/.olympolabs/openrouter.json  {"api_key": "sk-or-v1-..."}   (enrich_llm.py)
  ~/.olympolabs/blitz.json       {"api_key": "blitz-..."}      (enrich_blitz.py)
Stages 1-6 use NO keys (public IRS data). 7-Zip must be installed for stage 6's
Deflate64 extraction (see gotchas): https://www.7-zip.org  (winget install 7zip.7zip)

================================================================
SCRIPT ARGS & EXAMPLES
================================================================
1) download_states.py  --out-dir DIR [--states ca,ny,tx] [--delay 1.5] [--base-url URL]
     python scripts/download_states.py --out-dir D:/Run/raw

2) merge_states.py  --raw-dir DIR --output FILE [--ein-col EIN]
     python scripts/merge_states.py --raw-dir D:/Run/raw --output D:/Run/merged.csv

3) filter_icp.py  --input FILE --output FILE [--subsection 03] [--deductibility 1]
     [--foundation-codes 10,11,15] [--min-revenue 800000] [--max-revenue 0]
     [--revenue-field REVENUE_AMT] [--keep-columns EIN,NAME,...]
     python scripts/filter_icp.py --input D:/Run/merged.csv --output D:/Run/icp.csv \
       --subsection 03 --deductibility 1 --foundation-codes 10,11,15 --min-revenue 800000 \
       --keep-columns EIN,NAME,STREET,CITY,STATE,ZIP,FOUNDATION,NTEE_CD,REVENUE_AMT,ASSET_AMT

4) classify_ntee.py  --input FILE --config noble.json --good-output FILE
     --no-ntee-output FILE --dropped-output FILE [--good-label good_feel]
     python scripts/classify_ntee.py --input D:/Run/icp.csv --config D:/Run/noble.json \
       --good-output D:/Run/good.csv --no-ntee-output D:/Run/no_ntee.csv \
       --dropped-output D:/Run/dropped.csv

5) dedupe_emailed.py  --input FILE [--input FILE ...] --emailed-file FILE
     --output FILE --overlap-output FILE
     python scripts/dedupe_emailed.py --input D:/Run/good.csv --input D:/Run/no_ntee.csv \
       --emailed-file D:/Run/emailed_eins.csv --output D:/Run/final.csv \
       --overlap-output D:/Run/overlap.csv

6) enrich_websites.py  --input FILE --output FILE --tmp-dir DIR
     [--years 2026,2025,2024] [--checkpoint FILE] [--7z PATH] [--wait 120]
     python scripts/enrich_websites.py --input D:/Run/final.csv \
       --output D:/Run/final_enriched.csv --years 2026,2025,2024 --tmp-dir D:/Run/.tmp

7) enrich_blitz.py  --input FILE --output FILE [--pilot 20] [--overlap-min 0.6]
     [--workers 8] [--base-url URL] [--key-file PATH]
     python scripts/enrich_blitz.py --input D:/Run/final_enriched.csv \
       --output D:/Run/final_blitz.csv --pilot 20          # pilot first
     python scripts/enrich_blitz.py --input D:/Run/final_enriched.csv \
       --output D:/Run/final_blitz.csv                     # full run

8) enrich_llm.py  --input FILE --output FILE [--pilot 20 | --full]
     [--model google/gemini-2.5-flash-lite:online] [--workers 5] [--timeout 40]
     [--source-tag gemini_flash_lite]
     python scripts/enrich_llm.py --input D:/Run/final_blitz.csv \
       --output D:/Run/final_llm.csv --pilot 20            # pilot first
     python scripts/enrich_llm.py --input D:/Run/final_blitz.csv \
       --output D:/Run/final_llm.csv --full                # full run

GATE the paid stages (7, 8): run --pilot first, show the user accept/reject samples,
WAIT for OK before the full run. Both checkpoint per-100 and resume on re-run.

================================================================
NOBLE-CAUSE CONFIG (the per-client knob — see templates/)
================================================================
classify_ntee.py reads a JSON config (NOT Bryan's hardcoded rules):
  good_letters        first NTEE letter -> good_feel
  drop_letters        first NTEE letter -> not_good_feel
  drop_override_code3 code3 values forced to drop even under a good letter
  drop_suffixes       any code3 ending in these -> drop (alliance/advocacy/research)
  keep_override_code3 code3 values rescued to keep even under a drop letter
  blank_ntee.foundation_keep  blank-NTEE rows kept as good_feel if FOUNDATION matches
The example in templates/noble-causes-example.json encodes Design Desk's "noble
causes" rules WITH inline notes. Build a fresh config per client from their ICP.

================================================================
DATA SOURCE & KNOWN GOTCHAS (the hard-won value)
================================================================
IRS EO BMF (filter source):  https://www.irs.gov/pub/irs-soi/eo_{st}.csv
IRS 990 e-file XML (websites): https://apps.irs.gov/pub/epostcard/990/xml/{year}/
  - index_{year}.csv  cols: EIN, OBJECT_ID, XML_BATCH_ID
  - {XML_BATCH_ID}.zip contains {OBJECT_ID}_public.xml ; website = WebsiteAddressTxt

  * DEFLATE64: the largest batches are compressed with Deflate64 (method 9). Python
    zipfile LISTS them but cannot DECOMPRESS ("compression method not supported");
    bsdtar/libarchive can't open them at all. enrich_websites.py uses 7-Zip for
    every batch. Without 7-Zip those batches yield 0 sites. 7z extracts with -r so
    members nested in a subfolder still match.
  * IRS CASING TYPO: some index XML_BATCH_ID values are lowercase (e.g. "..._04a")
    while the published ZIP is uppercase -> 404. The downloader retries uppercased.
  * SILENT TRUNCATION: big ZIPs are sometimes served without Content-Length; a
    dropped connection truncates them but they still "open". The downloader verifies
    Content-Length AND re-opens the ZIP before trusting it.
  * MISSING OBJECT_IDs: some OBJECT_IDs the index assigns to a batch are physically
    absent from that ZIP (IRS-side index/content mismatch) — UNRECOVERABLE, expected.
  * EIN normalization (strip, dashes, upper, string) on BOTH sides everywhere — the
    IRS stores "12-3456789", your list may have "123456789".
  * Checkpoint after every batch + an outer resume loop; built for flaky connections.

================================================================
EXPECTED COVERAGE & GATING
================================================================
  IRS 990 waterfall alone        ~90% of the fresh list get a website
  + Blitz fallback               a few more % (≈20-35% hit on the remaining blanks)
  + LLM :online fallback         to ~97% total (≈60% accept on remaining blanks)
The remaining ~3% have no findable official site (paper filers / genuinely blank).
Blitz bills ~1 credit per query that returns a result; gate it with a pilot. LLM
:online has a per-search cost; gate it with a pilot and keep concurrency at 5.

================================================================
NOTES FOR CLAUDE CODE
================================================================
  - Confirm you have the ICP filters + noble_causes JSON + emailed file before
    starting. If any is missing, ASK — do not assume Design Desk's spec.
  - Run stages in the FOREGROUND; pilot the paid stages (7, 8) and WAIT for OK.
  - Forward slashes in paths on Windows; PYTHONIOENCODING=utf-8.
  - Stage 6 needs 7-Zip installed (Deflate64). Verify before the full enrich.
  - Keys from ~/.olympolabs/*.json only. Never commit client data or keys.
