---
name: conference-scraper
description: Scrape exhibitor, sponsor, partner, and speaker lists from trade-show / conference websites into a standardized 14-column lead CSV, then split and roll up into one deduped master file. Multi-platform — auto-detects the event-tech platform (MapYourShow, Informa a2z/eBooth, ExpoPlatform, Cvent, Swapcard, RainFocus, Bizzabo, etc.) and uses the matching scraper; falls back to headless Playwright for JS-rendered SPA sites. Use whenever the user wants to "scrape a conference", "get the exhibitor list", "pull sponsors from this trade show", "build a lead list from an expo", "who's exhibiting at X", "scrape the show floor directory", or turn a list of conferences into a companies-to-contact CSV. No login, no credentials, no CAPTCHA solving — public directories only. Node.js 18+ and PowerShell; run `npm install` in scripts/ once (auto-installs Playwright + Chromium).
---

# Conference Scraper

A multi-platform pipeline that turns conference / trade-show websites into B2B lead lists — the **companies exhibiting, sponsoring, and partnering** plus **speakers and their employers**. Output is a canonical 14-column CSV per conference, then a split + deduped master rollup across all conferences.

Node.js does the scraping; PowerShell does the CSV post-processing. **No credentials, no login, no API keys** — it only reads publicly published exhibitor/sponsor directories.

## When to use

The user has one or more conference URLs and wants the list of companies involved, as a lead list. Works best when the event publishes a public "Exhibitors" / "Sponsors" / "Show Floor" directory.

## Prerequisites & one-time setup

- **Node.js 18+** and **PowerShell 5+** (Windows).
- Install dependencies once:
  ```powershell
  cd scripts
  npm install      # postinstall auto-runs: npx playwright install chromium
  ```

## The scripts and their inputs

All Node scripts take **positional command-line arguments** (no prompts, no secrets). PowerShell scripts take a required `-Root`.

| # | Script | Input | Output |
|---|--------|-------|--------|
| 1 | `01_triage.js` | `node 01_triage.js sites.csv` — CSV of `name,url` lines (no header). Or edit the `SHOWS` array and run with no arg. | Prints each site's detected platform + candidate exhibitor/sponsor menu links. No files written. |
| 2 | `02_mapyourshow_scraper.js` | `node 02_... <slug> <version> <outDir> <confName> <confDate> <city> <state>`<br>e.g. `pack26 8_0 "C:\out\PACK26" "PACK EXPO 2026" "Nov 2-5 2026" Chicago IL` | `<outDir>/exhibitors_speakers.csv` + `notes.txt`. Pure HTTP, no browser. Richest data (website, LinkedIn, description, speakers). |
| 3 | `03_informa_ebooth_scraper.js` | `node 03_... <baseUrl> <listPath> <outDir> <confName> <confDate> <city> <state>` | same output. For Informa a2z classic `eBooth.aspx` shows. |
| 4 | `04_header_menu_hunt.js` | `node 04_... <homepage_url>` | Prints exhibitor/sponsor/partner/floor links found in the site nav. Discovery step for unknown platforms. |
| 5 | `05_playwright_generic.mjs` | `node 05_... <url> <outDir> [--selector="h3.exhibitor-name"]` | `rendered.html`, `rendered.txt`, `api_*.json`, `selector_matches.txt`. Headless Chromium — for JS-rendered SPA sites (Dreamforce, G2E, ExpoPlatform…). |
| 6 | `06_split_csv.ps1` | `.\06_split_csv.ps1 -Root "C:\out\Conferences"` (Root **required**) | Splits each conference's `exhibitors_speakers.csv` into `exhibitors.csv`, `sponsors_partners.csv`, `speakers.csv`, `attendees.csv`. Expects conference subfolders named `NN_ConfName`. |
| 7 | `07_build_master.ps1` | `.\07_build_master.ps1 -Root "C:\out\Conferences"` (Root **required**) | `00_MASTER_all_companies.csv` — deduped rollup across all conferences (same company at different conferences = separate leads). |

## Typical flow for a new conference

1. **Triage** — `01_triage.js` tells you the platform.
2. **Scrape** — run the matching scraper:
   - MapYourShow (`*.mapyourshow.com`) → `02_*`
   - Informa a2z / `eBooth.aspx` → `03_*`
   - Unknown / SPA → `04_header_menu_hunt.js` to find the directory URL, then `05_playwright_generic.mjs`
   Each writes `exhibitors_speakers.csv` into its `NN_ConfName` output folder.
3. **Split** — `06_split_csv.ps1 -Root <folder>` → 4 typed files per conference.
4. **Master** — `07_build_master.ps1 -Root <folder>` → one deduped master lead file.

## Output format (canonical — do not change)

`exhibitors_speakers.csv` has 14 columns:
```
Conference Name,Event Date,City,State,Country,Type,Name,Company,Company Website,LinkedIn URL,Company Description,Title/Role,Source URL,Notes
```
`Type` ∈ `Exhibitor | Sponsor | Partner | Speaker | AttendeeStat`. For companies, fill `Company` (leave `Name` blank). For speakers, fill `Name` (person) + `Company` (employer).

## Guardrails (baked into the process — keep them)

- **No CAPTCHA solving, no login, no account creation** — auth-gated content stays out. If a page requires login, stop and note it.
- **No proxy rotation** to defeat rate limits — reduce concurrency instead.
- **No fabrication** — if a site is blocked, write `0 found — site 403` in `notes.txt`. Never invent company names.

## Runtime notes

- MapYourShow detail fetches: 8 parallel @ 250ms pacing. HTTP 202 / empty body = throttled → back off ~5 min, don't hammer.
- MapYourShow AJAX needs the `X-Requested-With: XMLHttpRequest` header (403 without it) — already set in the script.
- Git Bash on Windows: prefix commands with `MSYS_NO_PATHCONV=1` when passing URL paths as args, or use backslash Windows paths, so `/Public/Foo.aspx` isn't rewritten to a filesystem path.
- Extraction depends on each platform's current markup/endpoints; if a site changes, re-run `01_triage.js` to redetect. See `SCRAPING_PROCESS.md` for the full A-to-Z playbook and `README.md` for the failure-mode table.
