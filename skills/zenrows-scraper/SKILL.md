SKILL NAME: ZenRows Website Scraper
VERSION: 5.0
AUTHOR: OlympoLabs

DESCRIPTION:
Scrapes homepage content for every company in a CSV. Tries a FREE
plain-httpx fetch first, then falls back to the ZenRows API only for
domains httpx can't get. Auto-detects website and company name columns.
All scraped content (httpx and ZenRows) is run through html2text, so
homepage_content always holds clean readable text — never raw HTML.

4-phase cost-optimized flow (Phase 0 is free, Phases 1-3 are paid):
- Phase 0: httpx + html2text (FREE, ~65% success rate). Plain GET
  with a 12s timeout, follows redirects, rotating user agents. HTTP
  200 with non-thin readable text is used as-is and ZenRows is
  skipped. Anything that fails / is non-200 / is thin falls through
  to Phase 1. Runs at the user concurrency (default 30).
- Phase 1: ZenRows basic — cheap request (autoparse=false,
  antibot=false, js_render=false, premium_proxy=false), $0.00013 per
  request, concurrency 30 (default, hard cap 40). Runs on httpx
  failures only.
- Phase 2: ZenRows antibot — retry Phase 1 failures with
  autoparse=true, antibot=true (still no JS, no premium) at
  concurrency 25.
- Phase 3: ZenRows JS render — retry Phase 2 failures with full
  stack — autoparse=true, antibot=true, js_render=true,
  premium_proxy=true — at concurrency 25. Anything still failing is
  marked scrape_failed.

The scraper is already implemented as scraper.py in this repo.
Claude Code DOWNLOADS and RUNS that file. It does NOT rewrite,
regenerate, or "improve" it.

================================================================
IMPORTANT FOR CLAUDE CODE — READ FIRST
================================================================
1. Do NOT build a Python script from scratch. The script
   already exists at scraper.py in this skill folder. Just
   download and run it.
2. Run the script directly in the terminal (FOREGROUND) so the
   tqdm progress bar is visible. Do NOT use Start-Process,
   -WindowStyle Hidden, or any other background method.
3. The script reads ZENROWS_API_KEY from the environment. Do
   NOT hardcode it, prompt for it, or pass it as an argument.
4. NEVER use backslashes in paths passed on the command line.
   Convert D:\folder\file.csv to D:/folder/file.csv before running.
5. Download scraper.py into the SAME folder as the input CSV.
   NEVER write it to C:/Users/DELL/ or D:/ root.
6. Cache and output files MUST live in the CSV folder.
   Never under C:/Users/ or a drive root.
7. The cache DB (scrape_cache.db) is the source of truth.
   The output _scraped.csv is rebuilt from cache; delete it
   before restarting (see "Restart Rules" below).

================================================================
USER-FACING SETUP (one time only)
================================================================
The user runs this once in a terminal:

    setx ZENROWS_API_KEY "your_zenrows_key_here"

Then closes and reopens the terminal so the env var is visible
to new processes.

================================================================
USER-FACING USAGE (every run)
================================================================
The user opens Claude Code and types:

    Read this skill and run it:
    https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/zenrows-scraper/SKILL.md

    CSV: D:\My Folder\companies.csv

Claude Code then performs the steps below WITHOUT asking
follow-up questions and WITHOUT rewriting the script.

================================================================
STEPS CLAUDE CODE MUST FOLLOW
================================================================

STEP 1 — Download scraper.py into the CSV folder
-------------------------------------------------
Compute {csv_folder} from the user-provided CSV path
(everything before the final filename), using forward slashes.

Run exactly:

    curl -fsSL -o "{csv_folder}/scraper.py" \
      "https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/zenrows-scraper/scraper.py"

If curl is unavailable, fall back to:

    wget -O "{csv_folder}/scraper.py" \
      "https://raw.githubusercontent.com/ranamuneebsb/Olympo-labs-skills/main/skills/zenrows-scraper/scraper.py"

STEP 2 — Read API key from environment
---------------------------------------
The script reads ZENROWS_API_KEY from the environment itself.
Do NOT prompt the user for it. Do NOT hardcode it.

If ZENROWS_API_KEY is not set, the script will print setup
instructions and exit. In that case, tell the user to run:

    setx ZENROWS_API_KEY "your_zenrows_key_here"

then close and reopen the terminal, and try again.

STEP 3 — Run the script immediately (FOREGROUND)
-------------------------------------------------
Run directly in the terminal, exactly:

    python "{csv_folder}/scraper.py" "{csv_path}" --concurrency 30

This MUST run in the foreground so the user can see the live
tqdm progress bar. Do NOT use Start-Process, do NOT pass
-WindowStyle Hidden, and do NOT background the process with
& or any other method. Run it directly in the terminal as
shown above.

Notes:
- {csv_path} uses forward slashes.
- The CSV path is positional. Do NOT pass --csv-path.
- The API key is taken from the environment (ZENROWS_API_KEY).
  Do NOT pass it as an argument.
- Default concurrency is 30. Hard cap is 40 (see "Concurrency
  Limits" below).

Optional flags:
- --concurrency N : override Phase 1 concurrency (default 30,
  hard cap 40). Phase 2 and Phase 3 clamp themselves to this
  value when it's lower than their per-phase cap of 25.
- --force : skip the 15-minute restart cooldown (only after
  probe_resume.py confirms the WAF is clear — see Restart Rules).

================================================================
RESTART RULES — READ BEFORE STOPPING OR RESTARTING
================================================================

1) 15-MINUTE COOLDOWN AFTER ANY KILL/RESTART
   ZenRows' edge WAF blocks the source IP for 10-15 minutes
   after a hard stop or rapid restart. Symptom: 100% Failed
   with 0 OK and 0 DD (no HTTP responses arrive at all — pure
   TCP-close / RemoteDisconnected).

   Rule: wait at least 15 minutes between any stop and the
   next start. scraper.py records the last start time in
   `{csv_folder}/.zenrows_last_run` and will refuse to launch
   inside the 15-minute window unless you pass `--force`.
   Use `--force` only after you've actually waited (e.g. after
   running probe_resume.py and confirming responses arrive).

2) PROBE BEFORE RELAUNCHING IF UNSURE
   If you don't remember when you last stopped, or you killed
   the process abruptly, run probe_resume.py first:
       python "{csv_folder}/probe_resume.py" "{csv_path}"
   It hits ZenRows with 3 URLs. ≥1 HTTP response = WAF clear,
   safe to launch. 0/3 = still blocked, wait longer.

3) DELETE THE OUTPUT CSV BEFORE RESTART
   The output `<basename>_scraped.csv` is in append mode. If
   you restart while it still exists, the pre-Phase-1 cached
   rows get appended on top of the previous run's rows →
   duplicate rows for every cached URL.

   Cache (scrape_cache.db) is the source of truth — NOT the
   output CSV. The scraper will rebuild the output CSV from
   cache on the next run.

   If you need to recover the output CSV without re-running
   the scraper, use export_from_cache.py:
       python "{csv_folder}/export_from_cache.py" "{csv_path}"

4) GHOST CONNECTIONS AFTER HARD KILL
   Killing the process with taskkill / SIGKILL leaves ZenRows
   counting your concurrency slots as in-use for ~10 minutes
   (Concurrency-Remaining: 0 even though nothing is running).
   Prefer Ctrl-C or `kill -INT` so the SIGINT handler closes
   sessions cleanly. If you had to hard-kill, wait the full
   15-minute cooldown before relaunching.

================================================================
CONCURRENCY LIMITS
================================================================
- Default: 30. Sustainable for hours at a time.
- Hard cap: 40. scraper.py clamps higher values down to 40 and
  prints a warning. Above 40 triggers ZenRows per-IP rate
  limiting within ~40 minutes.
- Above 80: Windows socket exhaustion errors.
- If you see sustained low success rate (<30% over 5k+ rows)
  while a fresh probe shows healthy responses, drop to 15-20
  rather than killing the run — the issue is per-IP
  throttling, not a code bug.

================================================================
WHAT THE SCRIPT DOES (reference only — do not reimplement)
================================================================
- Auto-detects website and company columns from CSV headers.
- Normalizes URLs (adds https://, strips www., strips trailing /).
- Converts every scraped page (httpx AND ZenRows) to clean text
  with html2text (ignore_links/images/emphasis, body_width=0).
  HTML is capped at 500 KB before conversion to avoid CPU spin.
  homepage_content is always readable text, never raw HTML.
- 4-phase flow (see "PHASES" section below for full settings):
  - Phase 0: FREE httpx fetch at user concurrency. HTTP 200 with
    non-thin text is used as-is and ZenRows is skipped. Failures
    fall through to Phase 1. ~65% typically clear here for $0.
  - Phase 1: ZenRows cheap basic scrape on httpx failures at user
    concurrency (default 30). CF blocks here are NOT permanent —
    they go to Phase 2.
  - Phase 2: antibot retry on Phase 1 failures at concurrency 25.
    CF blocks here go to Phase 3.
  - Phase 3: full-stack JS+premium retry on Phase 2 failures at
    concurrency 25. CF blocks here ARE cached permanent.
    Anything still failing is marked scrape_failed.
- Cache: scrape_cache.db (SQLite, WAL mode) saved next to the
  input CSV. Loads 70k entries in <0.5s and uses ~50 MB RAM
  (previous CSV cache loaded in 7 min and used 6-7 GB).
- Streaming writes: each successful row is appended to the
  output CSV the moment its future completes, and the HTML
  content is dropped from in-memory dicts immediately after.
  Steady-state RAM is ~1-3 GB even on 300k-row runs.
- Output: <csv_basename>_scraped.csv saved next to the input
  CSV. Append mode — delete before restart (see Restart Rules).
- Adds columns: homepage_content, homepage_status,
  homepage_content_length, scrape_phase.
- Permanent verdicts that won't be retried on next run:
    * `dead_domain` — Content-Type application/problem+json
      with HTTP status in {400, 404, 410, 413, 422}. Covers
      ZenRows RESP001 (parked), RESP002/007 (404),
      RESP005 (response too large).
    * `cloudflare_blocked` — only cached permanent from Phase 3
      (Phases 1 & 2 retry CF in the next phase). Detected from
      response body (ZenRows strips CF headers), 403 status, or
      200-with-empty-body.
- Transient errors that ARE retried on next run: timeouts,
  402 (quota), 429 (rate limit), 5xx, TCP-close.
- Graceful shutdown: SIGINT / SIGTERM sets a stop flag, waits
  up to 10s for in-flight requests, closes thread-local
  Sessions, and exits 0. Use Ctrl-C, not taskkill /F.

================================================================
PROGRESS DISPLAY
================================================================
IMPORTANT: Always run scraper.py in foreground, not background.
Running in background hides the tqdm progress bar. Claude Code
must NOT use Start-Process with -WindowStyle Hidden or any
background execution method. Run directly in the terminal so
the user can see the live progress bar.

The scraper prints (CF folded into "Failed" in Phases 1 & 2 so
the next phase can retry it with stronger settings):

    Phase 0: httpx (free)    X/Y | OK: A | Failed: C | ETA: Xmin
    Phase 1: ZenRows basic   X/Y | OK: A | DD: B | Failed: C | ETA
    Phase 2: ZenRows antibot X rows | New OK: A | Still failed: B
    Phase 3: ZenRows JS render X rows | New OK: A | Final failed: B
    Phase 0 (httpx free): A OK | C fell through to ZenRows |
      hit rate Z%
    Done: Total OK: X | Total Failed: Y | Saved to: [path]

The tqdm bar is coloured per phase: green (Phase 0 httpx), blue
(Phase 1 basic), yellow (Phase 2 antibot), red (Phase 3 JS).

CF = cloudflare_blocked, DD = dead_domain. Only Phase 3 caches
CF as a permanent verdict. DD is permanent in every phase. Phase 0
never produces CF/DD (no ZenRows), only OK or Failed.

httpx stats: the per-run summary and progress.json report
`httpx_ok` (domains fetched free), `httpx_failed` (domains that
fell through to ZenRows), and the httpx hit rate %.

================================================================
PHASES
================================================================
The site list is run through four phases. Phase 0 (free httpx)
clears the easy majority of domains for $0; Phase 1 trades
robustness for ~10x lower per-request cost on the rest; the two
later phases recover anything Phase 1 missed.

PHASE 0 — free httpx fetch + html2text
  Plain httpx GET. Timeout 12s, follow_redirects=True, rotating
  user agent (10 recent desktop/mobile UAs). Runs at the user
  concurrency (default 30, hard cap 40). The raw HTML is converted
  with html2text and accepted only if it yields >= 200 chars of
  readable text. HTTP 200 + non-thin text → used as-is, ZenRows
  skipped entirely; homepage_status is `ok_httpx`. Non-200,
  network error, empty, or thin → the row falls through to Phase 1.
  Cost: $0. Typical success rate ~65%. No ZenRows call is made, so
  cloudflare_blocked / dead_domain verdicts cannot occur here.

PHASE 1 — ZenRows cheap basic scrape (httpx failures only)
  autoparse=false, antibot=false, js_render=false,
  premium_proxy=false. Concurrency 30 (default, clamps to
  --concurrency if specified, hard cap 40). Cost: $0.00013 per
  request. Returns raw HTML for sites that respond without any
  bot challenge. CF blocks here are NOT cached as permanent
  verdicts — they go into the Phase 2 retry pool.
  homepage_status for OKs from this phase is `ok_basic`.

PHASE 2 — antibot retry
  autoparse=true, antibot=true, js_render=false,
  premium_proxy=false. Concurrency 25 (clamps to --concurrency
  if lower). Retries every Phase 1 failure (including CF blocks).
  CF blocks here are still NOT permanent — they go to Phase 3.
  homepage_status for OKs from this phase is `ok`.

PHASE 3 — full-stack retry
  autoparse=true, antibot=true, js_render=true,
  premium_proxy=true. Concurrency 25 (clamps to --concurrency
  if lower). Retries every Phase 2 failure. CF blocks here ARE
  cached permanent (last-resort). Anything still failing after
  Phase 3 is written with homepage_status = `scrape_failed`.
  homepage_status for OKs from this phase is `ok_js`.

Notes:
- The cache phase column stores 0, 1, 2, or 3 — phase 0 = free
  httpx, useful for telling httpx rows from ZenRows rows and
  raw-HTML Phase-1 rows from autoparsed Phase-2/3 rows.
- dead_domain is detected and cached permanent in every phase
  (it's a ZenRows verdict, not affected by antibot/JS).
- Restart rules and the 15-minute cooldown apply unchanged —
  they're about ZenRows' edge WAF, not the request settings.

================================================================
TROUBLESHOOTING
================================================================
- "ZENROWS_API_KEY not set" → user must run setx and reopen
  terminal.
- "Last run was N minutes ago — wait 15 min or pass --force"
  → genuine cooldown protection. Wait, then retry. Use
  --force only after probe_resume.py confirms the WAF is clear.
- 100% Failed with 0 OK and 0 DD → edge WAF block. Stop,
  wait 15 min, run probe_resume.py, then relaunch.
- 429 rate limit → reduce --concurrency (try 15 or 10).
- Sustained <30% success while probe is healthy → reduce
  concurrency to 15-20 instead of killing.
- Many failures in Phase 3 → likely dead domains, not bugs.
- Concurrency-Remaining: 0 with no scraper running → ghost
  connections from a hard kill. Wait 10-15 min.
- Duplicate rows in output CSV → restart-without-delete. Stop,
  delete `<basename>_scraped.csv`, restart (cache repopulates
  it on the cached_rows pre-Phase-1 pass). Or use
  export_from_cache.py.
- Output CSV is 0 bytes for 1-3 min after launch with a
  populated cache → normal. The pre-Phase-1 cached_rows loop
  is running; stdout is block-buffered until the first
  Phase-1 progress line forces a flush. Check the file's
  mtime to confirm activity.
