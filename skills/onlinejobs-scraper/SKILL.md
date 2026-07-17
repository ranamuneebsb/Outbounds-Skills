---
name: onlinejobs-scraper
description: Scrape fresh job postings from OnlineJobs.ph (Philippines remote-work board) into a CSV of leads — job title, category, salary, employer email, company name, and website — filtered to jobs posted on/after a cutoff date. Use whenever the user wants to "scrape OnlineJobs.ph", "pull VA job leads", "get employers hiring on onlinejobs", "find companies hiring virtual assistants in the Philippines", "grab fresh OJ job postings", or build a B2B lead list from OnlineJobs.ph employers. Two-phase async scraper (~200 keywords): Phase 1 collects fresh job stubs, Phase 2 scrapes detail pages for contact info. Each runner supplies their OWN OnlineJobs.ph login at runtime (prompted, or OJ_EMAIL/OJ_PASSWORD env vars) plus a cutoff date (OJ_CUTOFF). Requires: pip install httpx pandas tqdm.
---

# OnlineJobs.ph Fast Scraper

Two-phase async scraper that pulls **fresh** job postings from [OnlineJobs.ph](https://www.onlinejobs.ph) and extracts employer contact info for B2B lead-gen. Output is a timestamped CSV.

## When to use

The user wants a lead list of companies actively hiring on OnlineJobs.ph — typically employers hiring VAs, marketers, developers, customer-service reps, etc. The scraper harvests the employer's **email, company name, and website** from each job description.

## Prerequisites

1. **Python + dependencies** (run once):
   ```
   pip install httpx pandas tqdm
   ```
2. **A valid OnlineJobs.ph account.** The script logs in to see job details. Each runner uses their OWN login — no credentials are stored in this skill.

## How to run

```
python scripts/scrape_fast.py
```

The script **prompts for three things** at startup (no file editing needed):

| Prompt | Meaning | Env-var override |
|--------|---------|------------------|
| `OnlineJobs.ph email:` | The runner's account email | `OJ_EMAIL` |
| `OnlineJobs.ph password (hidden):` | The account password (input is hidden) | `OJ_PASSWORD` |
| `Cutoff date YYYY-MM-DD [default]:` | Keep only jobs posted **on/after** this date | `OJ_CUTOFF` |

For automated / non-interactive runs, set the env vars first so nothing is prompted:
```
# PowerShell
$env:OJ_EMAIL="you@example.com"; $env:OJ_PASSWORD="secret"; $env:OJ_CUTOFF="2026-07-11"
python scripts/scrape_fast.py
```

On Windows, if you see garbled characters, run with `PYTHONIOENCODING=utf-8`.

## What it does

- **Phase 1 — collect stubs:** fires ~200 keywords in parallel (concurrency 170), walks up to 10 listing pages per keyword, and stops paginating a keyword as soon as it hits a page of jobs older than the cutoff (uses the listing page's `data-temp` date — no detail fetch needed). Dedupes by job id. Saves `fast_stubs.json`.
- **Phase 2 — scrape details:** fetches each fresh job page (concurrency 100, 429 retry w/ exponential backoff), and extracts fields from the page. Email/company/website are pulled **only** from the `job-description` section (never the full page — the navbar leaks the logged-in email). The runner's own login email is auto-blocklisted.

## Output (in `scripts/`, next to the script)

- `jobs_from_<cutoff>_<timestamp>.csv` — the leads, UTF-8-sig (Excel-friendly), newest first. Columns: `job_id, title, job_category, type_of_work, salary, hours_per_week, date_updated, company_name, company_website, email, skills, description, job_url, keywords_matched`.
- `fast_stubs.json` — checkpoint of fresh job URLs found in Phase 1.
- `process_log.md` — appended run summary (total jobs, emails found, duration).
- `oj_checkpoints_fast/` — checkpoint dir.

Typical run: under ~30 minutes for fresh jobs only.

## Tuning

- **Keywords** — edit the `KEYWORDS` list in the script to change what's searched.
- **Concurrency** — `CONCURRENCY_LISTING` (170) and `CONCURRENCY_DETAIL` (100). Do **not** raise detail concurrency much past 100: the site rate-limits hard and at 400 nearly everything gets silently 429'd (author saw scrape drop from 10k+ jobs to ~259).

## Known limits

- Regex-based extraction is best-effort — `company_name`/`company_website` can be blank or approximate on messy listings. `email` is the most reliable field.
- Depends on OnlineJobs.ph's current HTML class names (`job__title`, `data-temp`, `job-description`). If the site changes markup, fields may come back empty.
- Uses `verify=False` (TLS verification off) to avoid cert hiccups against the site.
- No Phase-2 resume: a re-run repeats Phase 1 from scratch (it's fast, so this is fine).
- Respect OnlineJobs.ph's terms of service and rate limits when using this.
