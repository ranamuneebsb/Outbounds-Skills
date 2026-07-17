# Conference Scraper — script package

Share this whole `SCRIPTS/` folder with anyone who needs to re-run the scraper or add new conferences.

## What's in here

| File | Language | When to use |
|---|---|---|
| `README.md` | — | Start here |
| `SCRAPING_PROCESS.md` | — | Full A-to-Z playbook — read once |
| `01_triage.js` | Node.js | Detect which platform each conference uses (run first for a new list) |
| `02_mapyourshow_scraper.js` | Node.js | For any `*.mapyourshow.com` show (fastest, richest data) |
| `03_informa_ebooth_scraper.js` | Node.js | For any `eBooth.aspx`-style Informa a2z classic show |
| `04_header_menu_hunt.js` | Node.js | For unknown-platform sites — finds sub-page URLs from the header nav |
| `05_playwright_generic.mjs` | Node.js + Playwright | JS-rendered sites (SPAs) — Dreamforce, G2E, ExpoPlatform, etc. |
| `06_split_csv.ps1` | PowerShell | After scraping, split the combined CSV into 4 category files per conference |
| `07_build_master.ps1` | PowerShell | Roll up all conferences into one deduped master CSV |
| `package.json` | — | Node dependencies (Playwright only) |

## Setup once

Requires: **Node.js 18+**, **PowerShell 5+**, optionally **Python 3.10+** for HTML parsing helpers.

```powershell
cd scripts
npm install                    # installs playwright + chromium (via postinstall)
```

## Adding a new conference (typical flow)

1. Add the conference to a CSV list with columns `name,url`
2. Run `01_triage.js` — it prints which platform each site uses
3. Pick the matching script based on the platform:
   - MapYourShow → `02_mapyourshow_scraper.js <slug> <version> <outdir> <name> <date> <city> <state>`
   - Informa eBooth → `03_informa_ebooth_scraper.js <baseUrl> <listPath> <outdir> ...`
   - Unknown → `04_header_menu_hunt.js <url>` to discover the exhibitor/sponsor page, then use `05_playwright_generic.mjs`
4. Each script writes `<outdir>/exhibitors_speakers.csv` and `<outdir>/notes.txt`
5. Run `06_split_csv.ps1` to split the combined CSV into 4 typed files
6. Run `07_build_master.ps1` to rebuild the master file

## Do NOT do

- **No CAPTCHA solving.** If a page shows one, stop and note it.
- **No login / no account creation.** Auth-gated content stays out.
- **No proxy rotation** to defeat rate limits. Reduce concurrency instead.
- **No fabrication.** If a site is blocked, write "0 found — site 403" in notes.txt. Do not invent company names.

## What happens if a conference site changes

The **process is site-agnostic** — but the specific URL / platform may need to be redetected. Three failure modes and what to do:

| What changed | Symptom | Fix |
|---|---|---|
| **Same URL, still same platform** | Nothing — scripts just work | No action needed |
| **URL was restructured** (e.g. `/exhibitors` moved to `/companies/directory`) | 404 on the old path | Re-run `01_triage.js` and `04_header_menu_hunt.js` to find the new URL. The scraping technique stays the same. |
| **Platform was swapped** (rare but happens — e.g. site moved from a2z to MapYourShow) | Old platform signature no longer matches | Re-run `01_triage.js` — it will identify the new platform. Then use the matching scraper (`02_*` for MapYourShow, `03_*` for Informa, etc.). |
| **Anti-bot measures tightened** (Cloudflare Under Attack mode enabled) | HTTP 403 from every request, even with browser headers | Escalate to `05_playwright_generic.mjs`. If that's also blocked, the site is deliberately preventing scraping — document in notes.txt and move on. |
| **Site now requires login** | Content only visible when authenticated | Stop. Do not log in. Note it and move on. |

**Rule of thumb:** if a conference has an exhibitor list published publicly (menu → "Exhibitors" or "Show Floor"), one of these 5 scripts will get it. The maintenance work is running triage and picking the right script — not rewriting scrapers.

## Output format (canonical, do not change)

Every scraper writes `exhibitors_speakers.csv` with these 14 columns:

```
Conference Name,Event Date,City,State,Country,Type,Name,Company,Company Website,LinkedIn URL,Company Description,Title/Role,Source URL,Notes
```

`Type` is one of: `Exhibitor`, `Sponsor`, `Partner`, `Speaker`, `AttendeeStat`.
For companies, put the name in `Company` (leave `Name` blank).
For speakers, put the person in `Name` and their employer in `Company`.

## Runtime constraints observed in this codebase

- Concurrency cap for MapYourShow detail fetches: **8 parallel with 250ms pacing** (higher gets throttled).
- Rate-limit handling: HTTP 202 with empty body = throttled — back off 5 min, do not retry immediately.
- Windows / Git Bash gotcha: prefix commands with `MSYS_NO_PATHCONV=1` when passing URL paths as arguments, or use Windows-style backslash paths — otherwise `/Public/Foo.aspx` gets auto-converted to a filesystem path.
