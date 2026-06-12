SKILL NAME: Hiring Job Title Cleaning
VERSION: 1.0
AUTHOR: OlympoLabs

DESCRIPTION:
Cleans raw scraped job titles (job boards / Clay / LinkedIn scrapes) into two
cold-email merge variables:

  - clean_title:  canonical role title for
                  "I saw you're hiring for a {clean_title}"
  - short_title:  2-4 word conversational version
                  ("HR Director", "VP of HR", "CHRO")

The script is already implemented as scripts/clean_titles.py in this repo.
Claude Code RUNS that file with the user's CSV as the argument. It does NOT
rewrite, regenerate, or "improve" it.

================================================================
HOW IT WORKS
================================================================
STEP 1 — Rule-based cleaning (free, ~85% of rows)
-------------------------------------------------
Pure Python, no LLM. Strips locations (states, cities, regions, postal
codes), company names (after "@" / " at " / pipe segments), job IDs and
alphanumeric codes, shift / employment-type noise (full time, 2nd shift,
per diem, W2...), and bilingual notes. Fixes Sr./Jr. -> Senior/Junior,
expands VP -> Vice President, drops roman-numeral level markers, and
reorders department echoes ("Manager Human Resources" -> "HR Manager",
"Director Human Resources" -> "Director of Human Resources").

STEP 2 — LLM pass on the leftovers ONLY
---------------------------------------
Only rows the rules could not fully resolve are sent to the model — rows
still containing digits, 3+ capital runs (stray company acronyms), pipes,
or more than 6 words. Model: openai/gpt-4o-mini. Strict JSON response
format, temperature 0, with a do-not-guess error path: if the model cannot
produce a clean canonical title it returns an empty string rather than
hallucinating one. The same leftover-only logic runs again for short_title.

STEP 3 — Output
---------------
Writes {input}_cleaned.csv with three new columns — clean_title,
short_title, needs_review — plus a console report: clean->short samples,
a distinct short_title dedupe count, the rows flagged for manual review,
and the ACTUAL billed cost (read from OpenRouter usage.include, not an
estimate).

================================================================
USAGE
================================================================
    python scripts/clean_titles.py "INPUT.csv" \
        --col "Hiring Job Title" \
        [--no-llm] [--model X] [--concurrency N]

  INPUT.csv        positional; the scraped list to clean. Output is written
                   next to it as {input}_cleaned.csv.
  --col            the column holding raw titles (default "Hiring Job Title";
                   falls back to the first column if not found)
  --no-llm         rules only, skip STEP 2 entirely (free, no API calls)
  --model          OpenRouter model id (default openai/gpt-4o-mini)
  --concurrency    LLM workers (default 15, capped at min(rows, 30))

================================================================
API KEY — FILE ONLY, NEVER HARDCODED
================================================================
  ~/.olympolabs/openrouter.json   {"api_key": "sk-or-v1-..."}

The script reads the OpenRouter key from that file only. No key is hardcoded
and none is read from the environment. If the file is missing, the script
warns and continues rules-only (as if --no-llm).

================================================================
OUTPUT COLUMNS ADDED
================================================================
  - clean_title    canonical role title (may be "" if the LLM declined)
  - short_title    2-4 word conversational version
  - needs_review   "YES" when a row still looks dirty or came back empty

================================================================
HARD-LEARNED RULES (do not relearn these)
================================================================
- Rules first, LLM only for the judgment tail — 85% of title cleaning is
  deterministic. Do not send the whole list to the model.

- gemini-2.5-flash-lite REJECTED: it stalled twice under concurrent batch
  calls on OpenRouter (tail latency + burst throttling). gpt-4o-mini is the
  proven default. (Baked into PRIMARY_MODEL with a code comment.)

- Small batches (<500 rows): concurrency 15-30, never 100 — bursts trigger
  throttling that makes the run SLOWER, not faster. Hence default 15, hard
  cap min(rows, 30).

- 30s timeout — a 10-token title clean taking longer than that is a dead
  call, not a slow one. Max 2 retries, then move on.

- needs_review flag + a manual sheet fix beats debugging the automation for
  small one-off lists. Ship the 85%, hand-fix the flagged tail.
