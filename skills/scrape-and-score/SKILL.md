SKILL NAME: Scrape and Score
VERSION: 1.0
AUTHOR: OlympoLabs

DESCRIPTION:
One merged pipeline that qualifies a CSV of companies against an ICP and
writes scores back. It is description-first: a cheap LLM reads each
company's description and decides whether that is enough to score. Only
when the description is missing, thin, vague, or contradicts ICP keywords
in the company name does it scrape the website — and even then it tries a
FREE httpx fetch before spending a single ZenRows credit. Scraped pages
are scored by a stronger model. There is NO escalation model — scores are
final after description or scrape scoring.

This skill merges three earlier skills:
  - zenrows-scraper   (the httpx -> ZenRows scrape ladder + html2text)
  - icp-classifier    (CSV in / scored CSV + qualified CSV out)
  - company-scoring   (Bryan's exact prompts and 1-10 rubric)

The script is already implemented as scrape_and_score.py in this repo.
Claude Code DOWNLOADS and RUNS that file. It does NOT rewrite, regenerate,
or "improve" it.

================================================================
ICP PROMPT BUILDER (runs once, before scoring)
================================================================
You don't have to hand-write a detailed ICP. If the --icp value is SHORT
(fewer than 200 words), the script auto-expands it into a full scoring
rubric before the run:

  1. You pass a short description, e.g.:
       --icp "Personal injury law firms in the USA that represent injured
              people on a contingency fee basis"
  2. The script counts the words and sees it is under 200.
  3. It calls gpt-4o-mini ONCE to expand it into a complete ICP scoring
     prompt containing:
       - a clear definition of who qualifies
       - 3-5 YES examples with scores and reasons
       - 3-5 NO examples with scores and reasons
       - positive signals to look for
       - disqualifier signals
       - a 1-10 scoring guide
       - edge cases to watch for
  4. It prints the expanded prompt and asks:
       "Does this ICP look correct? Press Y to continue or N to cancel"
     (Press N to abort, refine your --icp text, and re-run.)
  5. The expanded prompt is what gets used for ALL scoring in the run.

If the --icp value is 200 words or more (typically an ICP profile .md
file), it is used AS-IS and no expansion happens.

Use --skip-icp-confirm to skip the confirmation prompt on unattended runs
(the expanded ICP is used without asking). On a non-interactive terminal
the script also proceeds automatically rather than hanging on input.

================================================================
WHAT IT DOES, IN PLAIN ENGLISH
================================================================
For every row in the input CSV:

  1. Read the company's description column.
  2. A cheap model (gpt-4o-mini) reads ONLY that description and returns a
     tentative 1-10 score, a confidence level, and a `needs_scrape` flag.
  3. Is the description clear enough to score?

     YES  -> use that score. NO scraping happens at all. This is the cheap,
             fast path and it handles the majority of rows.

     NO   -> the description is missing, thin, vague, ambiguous, OR the
             company NAME carries ICP keywords that the description seems to
             contradict. The row falls through to the scrape ladder, and the
             scraped text is then scored by a stronger model.

Scraped or not, every row ends up with a score, a verdict (Qualified /
Disqualified), the source of the score, and a one-line reason. A second
CSV containing only the Qualified rows is generated at the end.

================================================================
THE SCRAPE LADDER (only runs when a row needs scraping)
================================================================
PHASE 0 — httpx + html2text   (FREE)
  Async httpx GET (8s timeout, follows redirects, rotating user agent).
  The HTML is capped at 500 KB and converted to clean text with html2text
  (ignore_links / ignore_images / ignore_emphasis, body_width=0). If the
  cleaned text is substantial (>= 200 chars), the row is done and ZenRows
  is never touched. ~65% of sites typically clear here for $0.

PHASE 1 — ZenRows basic        (paid fallback)
  autoparse=true, antibot=false, js_render=false. Runs only when httpx
  failed, was blocked, returned non-HTML, or produced thin content.

PHASE 2 — ZenRows antibot + JS render combined   (paid, last resort)
  autoparse=true, antibot=true, js_render=true. Runs only when Phase 1
  also failed. This is the final attempt; if it fails too, the row is
  marked scrape_failed (scored 1 / Disqualified, "no usable site content").

All scraped content — httpx and ZenRows alike — is run through html2text,
so homepage_content always holds clean readable text, never raw HTML.

================================================================
WHEN SCRAPING TRIGGERS vs DESCRIPTION-ONLY
================================================================
Scraping is triggered when the description screen returns
`needs_scrape: true`, which the cheap model does when:
  - the description is missing or fewer than ~15 words and doesn't name
    the industry,
  - it's a generic placeholder ("Software company", "Consulting firm"),
  - it could plausibly be the right ICP or the wrong one (genuine
    ambiguity), or
  - the company NAME contains a core ICP industry keyword but the
    description would otherwise lead to a disqualify (highest-value case
    to verify by scraping).

Otherwise the row is scored straight from the description with no scrape,
and that score is final. There is no escalation model: a clear description
score is never re-checked by scraping, and a scrape score is never
re-scored by a second model.

================================================================
SCORING MODELS (Bryan's company-scoring logic, used exactly)
================================================================
  - Description screening:  openai/gpt-4o-mini
  - Scrape scoring:         deepseek/deepseek-chat-v3.1

No escalation model. Scores are final after description or scrape scoring.

Verdict: score >= threshold (default 7) => Qualified, else Disqualified.
Same 1-10 rubric and the same description/scrape system prompts as the
company-scoring skill.

================================================================
API KEYS — ALWAYS PASSED BY THE USER AT RUNTIME
================================================================
There are NO hardcoded keys anywhere in the code. The script does NOT
read keys from the environment, does NOT read ~/.olympolabs/ files, and
has NO fallback to any saved credentials. The user MUST pass keys as
command-line arguments every run:

    --openrouter-key   (always required)
    --zenrows-key      (required unless --no-zenrows or --description-only)

================================================================
COMMAND SYNTAX
================================================================
    python scrape_and_score.py "input.csv" \
      --icp "icp-profile.md" \
      --zenrows-key YOUR_ZENROWS_KEY \
      --openrouter-key YOUR_OPENROUTER_KEY \
      --concurrency 50 \
      --score-threshold 7

Positional:
  input.csv                Path to the input CSV (use forward slashes on
                           Windows: D:/folder/input.csv).

Required:
  --icp PATH_OR_TEXT       Path to an ICP profile (.md/.txt) OR inline ICP
                           text. If the value is under 200 words it is
                           auto-expanded into a full scoring rubric by
                           gpt-4o-mini before the run (see ICP PROMPT
                           BUILDER above); 200+ words is used as-is.
  --openrouter-key KEY     OpenRouter API key.
  --zenrows-key KEY        ZenRows API key (omit only with --no-zenrows
                           or --description-only).

Speed / concurrency:
  --concurrency N          Async row workers / httpx concurrency. Default 50.
  --zenrows-concurrency N  Max concurrent ZenRows requests. Default 30.
  --score-workers N        LLM thread-pool workers. Default 30.

Scoring:
  --score-threshold N      Score >= N => Qualified. Default 7.
  --description-model M     Default openai/gpt-4o-mini.
  --scrape-model M          Default deepseek/deepseek-chat-v3.1.
  --escalation-model M      ADVANCED, optional. Off by default. When set, it
                            re-scores borderline 5-7 / weak-confidence SCRAPED
                            rows with model M on the same scraped text (no new
                            scrape). Omit it and no escalation ever happens.

Mode (default is description-first hybrid):
  --scrape-only             Scrape + score every row; skip the description path.
  --description-only        Never scrape; score from description only.
  --no-zenrows              Use the free httpx fetch only; never call ZenRows.

Run control:
  --force-rescore           Re-score rows already present in the output CSV.
  --skip-confirm            Skip the >1000-row cost confirmation prompt.
  --skip-icp-confirm        Skip the expanded-ICP confirmation prompt
                            (unattended runs use the expanded ICP as-is).
  --max-rows N              Cap rows processed (testing).
  --csv-out PATH            Override the output path.

================================================================
SPEED + RESILIENCE
================================================================
  - Async scraping with asyncio + httpx.AsyncClient: httpx at --concurrency
    (default 50, free), ZenRows capped at --zenrows-concurrency (default 30).
  - LLM scoring on a ThreadPoolExecutor (default 30 workers). Rows are
    scored the moment they're scraped — the pipeline does not wait for the
    whole list to finish scraping first.
  - Fast fail: httpx timeout 8s, ZenRows timeout 12s, 1 retry only.
  - SQLite scrape cache (scrape_cache.db) stores BOTH successful and failed
    scrapes; restarting skips any domain already scraped.
  - Results stream to the output CSV as each row finishes — nothing is held
    in RAM for the whole run. Resume-safe: re-running skips rows that
    already have an ICP Score (unless --force-rescore).
  - tqdm progress bar:
        Scraped: X | Scored: X | Qualified: X | httpx%: X free |
        ZenRows: X credits used | ETA: Xmin

================================================================
COST ESTIMATE
================================================================
Before processing the full list, the script prints a rough cost estimate
(LLM + ZenRows) based on the row count and conservative assumptions
(~40% of rows scrape, ~65% of those served free by httpx). For runs over
1000 rows on an interactive terminal it asks for a "yes" confirmation;
pass --skip-confirm for unattended runs. Real cost depends on how many
descriptions turn out to be clear enough to score without scraping — the
clearer the descriptions, the cheaper the run.

Rough rules of thumb:
  - A row scored from description only:        ~$0.0003
  - A row that scrapes (free httpx) + scores:  ~$0.001-0.002
  - A row that needs ZenRows + scores:         ~$0.003-0.006

================================================================
OUTPUT FILES (all written next to the input CSV)
================================================================
1. <input>_scored.csv
   All original columns plus:
     homepage_content, homepage_status, scrape_phase,
     ICP Score, ICP Verdict, Scoring Source, ICP Reasoning
   Streamed (append) as rows complete.

   homepage_status values: ok_httpx, ok_zenrows_basic, ok_zenrows_js,
   scrape_failed, or empty (scored from description, never scraped).
   scrape_phase: 0 (httpx), 1 (ZenRows basic), 2 (ZenRows antibot+JS),
   or empty.
   Scoring Source: description, scrape, scrape_failed,
   description_thin (--description-only), or failed.

2. <input>_qualified.csv
   Only rows with ICP Score >= threshold (default 7). Regenerated from the
   scored CSV after scoring completes (dedup-safe).

3. scrape_cache.db
   SQLite cache of every scraped domain (success and failure).

4. progress.json
   Rewritten every 100 scored rows:
     scraped_done, scored_done, qualified_count, httpx_hits,
     zenrows_calls, errors, last_updated

================================================================
EXAMPLE USAGE
================================================================
Score a Clay export against a saved ICP, default threshold 7:

    python scrape_and_score.py "D:/leads/clay_export.csv" \
      --icp "D:/icps/dental.md" \
      --openrouter-key sk-or-v1-xxxx \
      --zenrows-key zr-xxxx \
      --concurrency 50 \
      --score-threshold 7

Short inline ICP — auto-expanded into a full rubric, then confirmed (Y/N):

    python scrape_and_score.py "D:/leads/list.csv" \
      --icp "Personal injury law firms in the USA that represent injured \
             people on a contingency fee basis" \
      --openrouter-key sk-or-v1-xxxx \
      --zenrows-key zr-xxxx

Same, but unattended (skip the expanded-ICP confirmation prompt):

    python scrape_and_score.py "D:/leads/list.csv" \
      --icp "Dental practices in the US offering cosmetic dentistry" \
      --openrouter-key sk-or-v1-xxxx \
      --zenrows-key zr-xxxx \
      --skip-icp-confirm

Free-httpx-only (no ZenRows spend), inline ICP, lower threshold:

    python scrape_and_score.py "D:/leads/list.csv" \
      --icp "B2B SaaS companies in North America, 11-200 employees" \
      --openrouter-key sk-or-v1-xxxx \
      --no-zenrows \
      --score-threshold 6

Force-scrape every row (ignore descriptions) for a high-accuracy pass:

    python scrape_and_score.py "D:/leads/list.csv" \
      --icp "D:/icps/dental.md" \
      --openrouter-key sk-or-v1-xxxx \
      --zenrows-key zr-xxxx \
      --scrape-only

A crashed run resumes by simply re-running the same command — already-scored
rows are skipped via the output CSV, and already-scraped domains are served
from scrape_cache.db.

================================================================
NOTES FOR CLAUDE CODE
================================================================
  - Download scrape_and_score.py into the SAME folder as the input CSV;
    output files, cache, and progress.json all live in that folder.
  - Run in the FOREGROUND so the tqdm progress bar is visible. Do NOT use
    Start-Process / -WindowStyle Hidden / background execution.
  - Use forward slashes in paths on the command line.
  - Always pass --openrouter-key and --zenrows-key as arguments. Never
    hardcode them, never read them from the environment or saved files.
  - If the user passes a short --icp, the script expands it and (on an
    interactive terminal) asks Y/N before scoring. Show the user the
    expanded ICP. For a hands-off run, pass --skip-icp-confirm.
  - Auto-installs httpx, html2text, requests, and tqdm if missing.
