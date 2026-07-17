# Conference Scraping Process — A to Z

End-to-end playbook for scraping exhibitors / sponsors / partners / speakers from any B2B conference website. Order matters — cheapest technique first, escalate only when needed.

---

## Step 0 — Ground rules (never violate)

- **No CAPTCHA solving / bypass.** If a site presents one, log it and stop that path.
- **No login.** Never create an account, never submit auth, never reuse session cookies from an authenticated browser.
- **No proxy rotation** to defeat rate limits. Slow down instead.
- **No fabrication.** If a site is blocked, write `0 found — site blocked` in `notes.txt`. Never invent company names.
- **Respect robots.txt spirit** — for site-owner data (exhibitor lists intended to be public), scraping is fine. For attendee names, only capture what the site publishes publicly.

---

## Step 1 — Set up per-conference output folder

```
C:\Users\ML\Desktop\RAW\Conference\US\<NN>_<ConferenceSlug>\
  ├── exhibitors_speakers.csv    ← combined raw output
  ├── exhibitors.csv             ← split by type
  ├── sponsors_partners.csv
  ├── speakers.csv
  ├── attendees.csv
  └── notes.txt                  ← platform, technique, gaps, gating
```

**Canonical CSV header** (14 columns, keep identical across all conferences):

```csv
Conference Name,Event Date,City,State,Country,Type,Name,Company,Company Website,LinkedIn URL,Company Description,Title/Role,Source URL,Notes
```

- `Type` ∈ { `Exhibitor`, `Sponsor`, `Partner`, `Speaker`, `AttendeeStat` }
- For companies (Exhibitor/Sponsor/Partner): put the company name in the `Company` column, leave `Name` blank
- For people (Speaker): put person's name in `Name`, their employer in `Company`
- `Source URL` = the exact page you extracted from (not the homepage)

---

## Step 2 — Platform detection

Fetch the conference homepage with browser-realistic headers (see Step 3). Grep the HTML for these signatures — each maps to a specific fast-path recipe:

| Signature in HTML / URL | Platform | Recipe |
|---|---|---|
| `*.mapyourshow.com` subdomain | **MapYourShow** | Step 4A |
| `libs.a2zinc.net` or `s2*.a2zinc.net` | **a2z Events (Personify)** | Step 4B |
| `*.smallworldlabs.com` | **SmallWorldLabs** (a2z-owned) | Step 4B |
| `eBooth.aspx?BoothID=` links | **a2z classic EventMap** (Informa) | Step 4C |
| `informaconnect.com/<slug>/` | **Informa Sitefinity** | Step 4D |
| `events.jspargo.com` | **SPARGO eventmap** | Step 4A (same API pattern as MapYourShow) |
| `api-*.expoplatform.com` | **ExpoPlatform** | Step 4E |
| `*.bizzabo.com` or Bizzabo event ID | **Bizzabo** | Step 4F |
| `goexposoftware.com` iframe | **goExpo** | Step 4G |
| `cediaexpo.com` style Angular SPA | **Custom SPA** | Step 5 (Playwright) |
| None of above | **Custom / static HTML** | Step 5 (menu discovery) |

---

## Step 3 — The one fetch recipe that unblocks 90% of sites

Plain `fetch()` or `curl -A "Mozilla/5.0"` gets 403'd by Cloudflare on Gartner, LOUPE, ANA, Content Marketing World, Dreamforce, etc. The fix is browser-realistic **Sec-Fetch-*** headers, not just User-Agent:

```bash
curl -s -o page.html -L --compressed \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36" \
  -H "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8" \
  -H "Accept-Language: en-US,en;q=0.9" \
  -H "Sec-Fetch-Dest: document" \
  -H "Sec-Fetch-Mode: navigate" \
  -H "Sec-Fetch-Site: none" \
  -H "Upgrade-Insecure-Requests: 1" \
  "https://target.example.com/exhibitors"
```

Cloudflare fingerprints the **absence** of `Sec-Fetch-*` headers as bot traffic. Adding them (they're what real Chrome sends) passes silently on ~90% of "protected" sites.

---

## Step 4A — MapYourShow / SPARGO recipe

**Signal:** homepage links to `<slug>.mapyourshow.com/<version>/...`

**Bulk list via public AJAX endpoint** (no browser needed):

```
GET https://<slug>.mapyourshow.com/<ver>/ajax/remote-proxy.cfm?
    action=search&searchtype=exhibitorgallery&searchsize=50&start=<N>

Headers:
  X-Requested-With: XMLHttpRequest       ← MANDATORY — 403 without this
  Referer: https://<slug>.mapyourshow.com/<ver>/explore/exhibitor-gallery.cfm
  User-Agent: (browser UA)
```

Response is JSON: `DATA.results.exhibitor.hit[]` (each has `exhname_t`, `exhid_l`, `exhdesc_t`, `booths_la`, etc.). Loop `start=0,50,100,...` until you hit `DATA.totalhits`.

**Per-exhibitor detail page** for website + LinkedIn + phone + address:

```
GET https://<slug>.mapyourshow.com/<ver>/exhibitor/exhibitor-details.cfm?exhid=<id>
```

The rendered HTML embeds a Vue `contactinfov3` component with the contact data as a JS object literal. Extract with:

```js
const anchor = html.indexOf('contactinfov3 = Vue');
const window = html.slice(anchor, anchor + 4000);
const block = window.match(/data: function\(\)\{[\s\S]{0,20}?return \{([\s\S]*?)\r?\n\s*\}\r?\n\s*\},?\s*\r?\n\s*mounted/);
// Then regex each key: websiteValue, linkedInValue, facebookValue, twitterValue, phoneValue, addressValues
```

**Sessions/speakers** — same API, different `searchtype`:

```
searchtype=sessionfulllist
```

Each session has `speakername[]` + `speakercompany[]` arrays.

**Concurrency:** 8 concurrent detail fetches, 250ms delay between. If you see 202 empty responses = throttled — back off 5 min, retry. Never bypass throttle with proxies.

**Fallback if the API returns 403 anyway:** Some MapYourShow shows expose a PDF export at `/exhibitor-list.cfm?export=pdf` — used successfully for Farm Science Review (369 exhibitors from PDF text).

---

## Step 4B — a2z Events / SmallWorldLabs recipe

**Signal:** `libs.a2zinc.net` scripts, or `<slug>.smallworldlabs.com` subdomain.

**Exhibitor list** at plain `/exhibitors?page=N` (50 per page). **Company detail** at `/co/<slug>` — server-rendered plain HTML with:

```
What We Do
{description}
Website
{url}
Phone
{tel}
Address
{addr}
Facebook / Instagram / LinkedIn
{urls if present}
```

Extract with label-based regex: `Website\s*\n\s*(https?://\S+)` etc.

**Pagination gotcha:** `?page=N` sometimes ignored — SmallWorldLabs uses AJAX "load more" that changes on scroll. If `?page=2` returns the same as `?page=1`, the platform is session-bound — **fall back to Playwright** (Step 5) to click through pagination.

**Alternative discovery:** homepage may say `Exhibitor List (300+)` — that's your target count. If you only capture the first ~40 with static fetch, note the gap in `notes.txt` and use Playwright.

---

## Step 4C — a2z classic EventMap (Informa) recipe

**Signal:** exhibitor list page has `<a href="eBooth.aspx?BoothID=NNNN">Company Name</a>` links.

**List extraction** — pure regex on the list page HTML:

```regex
href="(eBooth\.aspx\?[^"]+)"[^>]*>\s*([^<]+?)\s*<
```

**Detail page** at `/Public/eBooth.aspx?BoothID=<id>` — plain text pattern near the company name:

```
Booth: NNNN  www.company.com  Contact This Exhibitor
City, ST
Country
```

Extract with:

```regex
Booth:\s*\S+\s+(www\.\S+|https?://\S+)?\s*Contact This Exhibitor
```

This platform gives you **website + city/state/country** per exhibitor but **no description and no LinkedIn** — that's a platform limit, not a scraping failure.

**Concurrency:** 4 parallel, 150ms delay.

---

## Step 4D — Informa Sitefinity (informaconnect.com) recipe

**Signal:** URL matches `informaconnect.com/<event-slug>/sponsors/` or `/speakers/`.

**Critical insight:** these pages show a `"You need to enable JavaScript"` banner but the sponsor/speaker data **is actually in the initial server-rendered HTML** — the banner is misleading. Fetch with browser-realistic headers (Step 3) and parse.

**Extraction technique** — the page uses tier-section headers as anchors:

1. Find the "Sponsors & Exhibitors" or "Platinum Sponsors" heading in the HTML
2. Scan the chunk between that anchor and "Additional Content" / footer
3. Extract text content between `>...<` where content matches `[A-Z][A-Za-z0-9 .,&'/-]{2,60}`
4. Filter out obvious non-companies (tier names, UI labels, dates, etc.)

**Sample find for BioProcess International:** raw HTML → text near "Platinum Sponsors" → 220 company names (ACROBiosystems, Altruist Biologics, Asahi Kasei Bioprocess, Avantor, Beckman Coulter, Benchling, Bio-Rad, Cytiva, Novartis, Samsung Biologics, etc.)

---

## Step 4E — ExpoPlatform recipe (IMEX pattern)

**Signal:** Next.js SPA with API calls to `api-*.expoplatform.com` in Network tab.

Public JSON endpoints exist but may require an auth token from the initial page load. Two approaches:

1. **Static (may fail):** try `GET https://api-<event>.expoplatform.com/api/mobile/companies?per_page=200`  
   Often returns 404 without token.

2. **Playwright (works):** load the exhibitors page, capture all XHR responses in `page.on('response')`, filter for JSON responses containing `exhibitor`/`sponsor`/`company` keywords, dump the bodies. The correct API is typically `/api/v1/exhibitors?page=N` with a bearer token in an `Authorization` header — the token is fetched fresh on page load.

---

## Step 4F — Bizzabo recipe

**Signal:** `data-bz-event` attribute or `bizzabo.com` iframe.

Event agenda + speakers are often exposed via a **separate organizer-owned page** (not the Bizzabo widget). Check for URLs like `conference.com/agenda/speakers` or `conference.com/page/12345/speakers` — these are frequently server-rendered even when the main event page is the Bizzabo widget.

Do NOT try to auth into the Bizzabo widget (breaks Step 0 rules).

---

## Step 4G — goExpo (goExposoftware) recipe

**Signal:** `<iframe src="https://<region>.goexposoftware.com/...">`.

The iframe URL is directly fetchable. Its content is a plain HTML table of all exhibitors + per-exhibitor profile pages with description and website. Used successfully for San Diego Comic-Con (711 exhibitors, 100% coverage).

---

## Step 5 — Custom / JS SPA / everything else: header-menu-hunt + Playwright

When no known platform is detected:

### 5A. Homepage menu discovery (before touching a browser)

1. Fetch homepage with Step 3 headers
2. Regex ALL `<a href="X">Text</a>` where either `href` or `text` contains any of:  
   `exhibitor | sponsor | attendee | partner | floor | expo | who-is | show-floor | companies`
3. Follow each candidate URL — you'll typically find a real `/exhibitors`, `/sponsors`, `/who-is-sponsoring`, `/attendee-list`, `/event-partners` etc. that wasn't in the obvious guess-list

**Example wins from this technique:**
- Gartner: found `/en/conferences/na/symposium-us/exhibitors` (182 exhibitors in `<h3 class="exhibitor-heading">`)
- ANA Masters: found `/content/show/id/ms-anc-oct26` (65 sponsor logos with `alt="companyname"`)
- Content Marketing World: found `/event-partners/` (23 partners in anchor text)
- BioProcess: found `informaconnect.com/bioprocessinternational/sponsors/` (link labeled "Meet the Exhibitors")

### 5B. Content extraction — 4 signals in order

For each page, try these extractors in order until one works:

1. **`<h3 class="exhibitor-*">`** or `<h3 class="company-*">` — cleanest, when present
2. **`<h3>` / `<h4>` inside `<article>` or main content area** — often used by WordPress sites
3. **`alt="X logo"` on `<img>` tags** — strip " logo" suffix, most sponsor-logo grids use this pattern (ANA, INBOUND, CMW partners)
4. **Anchor text near sponsor-classed containers** — `<a href="...sponsor..." class="...sponsor...">Company</a>`
5. **Embedded JSON blobs** — `__NEXT_DATA__`, `window.__PRELOADED_STATE__`, `apolloState` — parse and hunt for `"name"` fields
6. **Flattened text extraction** — as a last resort, use Playwright's `document.body.innerText` and parse tier headings + company blocks

### 5C. Playwright for JS-only content

When the page requires JS to render (React/Vue/Angular SPA), install Playwright and use it:

```js
import { chromium } from 'playwright';
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  viewport: { width: 1440, height: 900 }
});
const page = await ctx.newPage();

// Capture JSON API responses — often the real data source
const apiCalls = [];
page.on('response', async res => {
  const u = res.url();
  const ct = res.headers()['content-type'] || '';
  if ((u.includes('exhibit')||u.includes('sponsor')||u.includes('company')||u.includes('/api/'))
      && ct.includes('json')) {
    try { apiCalls.push({ url: u, body: await res.text() }); } catch(e){}
  }
});

await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(5000);          // let JS run
// Force lazy-loaded content by scrolling
for (let i=0; i<40; i++) {
  await page.evaluate(y => window.scrollBy(0, y), 900);
  await page.waitForTimeout(150);
}
// Click "Load more" if present
while (true) {
  const btn = await page.$('button:has-text("Load more"), button:has-text("Show more")');
  if (!btn) break;
  await btn.click();
  await page.waitForTimeout(1500);
}
await page.waitForTimeout(3000);

const html = await page.content();
const text = await page.evaluate(() => document.body.innerText);
// Save both — extract from HTML for structural signals, from text for readability
```

**Playwright wins from this session:**
- Dreamforce 2026 (`reg.salesforce.com/flow/plus/df26/sponsors/page/sponsorlisting`): 11 → 103 sponsors with descriptions
- G2E 2026 (`globalgamingexpo.com/en-us/attend/exhibitor-list.html`): 1 → 201 exhibitors from `<h3 class="exhibitor-name">`
- Content Marketing World rendered blocks: 8 → 40 sponsors

**Do not use Playwright for auth-gated pages.** If the page requires login, stop.

### 5D. When even Playwright fails

**Cloudflare "Under Attack" mode** blocks headless Chromium too (Ai4, ANA in some modes). No workaround respects Step 0. Document in `notes.txt` and move on.

**Alternative headless engines** if Playwright is heavy:
- [obscura](https://github.com/h4ckf0r0day/obscura) — Rust-based, 30 MB memory, CDP-compatible
- undetected-chromedriver
- puppeteer-extra-plugin-stealth

All three cost the same in effort but pick a lighter one if you're running many concurrent scrapes.

---

## Step 6 — Extract, dedupe, write CSV

### Company name cleanup rules

- Strip leading/trailing whitespace and quotes
- Strip trailing " logo" (from alt-text scrapes)
- Strip trailing " Sponsor" / " Exhibitor" (from tier headers accidentally matched)
- Preserve original case (`AWS`, not `aws`)
- Preserve special chars (`AT&T`, `E*TRADE`)

### Filter list — reject anything matching these

```
{logo, icon, image, arrow, background, skip, more info, sponsor, exhibitor,
 partner, platinum, gold, silver, bronze, event, tier, level, apex, strategic,
 global, media, associate, marketing, contact, join, become, learn, view,
 opportunity, question, click, copyright, register, book, hotel, travel,
 register now, become a sponsor, view all}
```

Also reject entries < 2 chars or > 80 chars, and pure-numeric strings.

### Deduplication key

Per-conference: `(conference_folder, lowercase(company_name))`.  
A company can appear at multiple conferences — those aren't duplicates, they're the same brand exhibiting at multiple shows. Master file preserves all such rows.

### Writing to CSV — use proper CSV writer

**Do NOT** use naive `split(',')`. Event Date fields often contain `"October 14-16, 2026"` — the comma inside a quoted string breaks naive splits. Use `Import-Csv` (PowerShell) or `csv.DictWriter` (Python) which handle quoting correctly.

---

## Step 7 — Split combined CSV into 4 category files

After writing the master `exhibitors_speakers.csv` per conference, split into 4 typed files. PowerShell recipe:

```powershell
$rows = Import-Csv (Join-Path $dir "exhibitors_speakers.csv")
$exh = $rows | Where-Object { $_.Type -eq "Exhibitor" }
$spo = $rows | Where-Object { $_.Type -in @("Sponsor","Partner","Community Partner","Village","Community","Contest Organizer","Organizer") }
$spk = $rows | Where-Object { $_.Type -eq "Speaker" }
$att = $rows | Where-Object { $_.Type -in @("Attendee","AttendeeStat") }
$exh | Export-Csv (Join-Path $dir "exhibitors.csv") -NoTypeInformation -Encoding utf8
$spo | Export-Csv (Join-Path $dir "sponsors_partners.csv") -NoTypeInformation -Encoding utf8
$spk | Export-Csv (Join-Path $dir "speakers.csv") -NoTypeInformation -Encoding utf8
$att | Export-Csv (Join-Path $dir "attendees.csv") -NoTypeInformation -Encoding utf8
```

**Type-normalization map** (agents sometimes use inconsistent Type values — normalize before splitting):

| Raw Type value | Normalize to |
|---|---|
| `Sponsor/Exhibitor` | `Sponsor` |
| `Exhibitor/Sponsor` | `Sponsor` |
| `Sponsor (Media Partner)` | `Partner` |
| `Community Partner` | `Partner` |
| `Village` (DEF CON) | `Partner` |
| `Community` (DEF CON) | `Partner` |
| `Contest Organizer` | `Partner` |
| `Speaker (Sponsor Rep)` | `Speaker` |
| `Speaker (unconfirmed...)` | `Speaker` |
| `Attendee Stats` | `AttendeeStat` |

Also: fall back to `Name` column if `Company` is blank — some agents put the company name in the wrong column.

---

## Step 8 — Master file across all conferences

After all 60 conferences complete, build one deduped-per-conference master CSV:

```
Conference Name | Event Date | City | State | Country | Type | Company | Company Website | LinkedIn URL | Company Description | Source URL | Notes
```

Dedupe key: `(conference_folder, lowercase(company_name))`. Same company at different conferences = separate rows (not duplicates).

Location: `C:\Users\ML\Desktop\RAW\Conference\US\00_MASTER_all_companies.csv`

---

## Step 9 — notes.txt per conference (always required)

Write plain-text notes documenting:

1. **Platform detected** (MapYourShow/a2z/custom/etc.)
2. **URLs used** (which exact pages you extracted from)
3. **Technique** (fast-path API, header-menu discovery, Playwright, etc.)
4. **Site-direct vs WebSearch enrichment breakdown** — e.g. "150/220 companies got website from platform, 70 enriched via WebSearch"
5. **Gaps** — what you tried but couldn't get, and why (site-blocked / login-gated / not-yet-published)
6. **Attendee stats** — any published aggregate figures (e.g. "10,000+ attendees, 400+ exhibitors") captured as `AttendeeStat` rows

Honest gap documentation is the point. `0 exhibitors — site returned 403 to all techniques` is better than fabricating names.

---

## Step 10 — Common failure modes and fixes

| Symptom | Cause | Fix |
|---|---|---|
| HTTP 403 on first request | Cloudflare, missing Sec-Fetch-* headers | Step 3 — add all Sec-Fetch headers |
| HTTP 202 empty body | Platform throttled you | Back off 5 min, reduce concurrency to 3 |
| Page loads, no exhibitor data | JS-rendered SPA | Step 5C — Playwright |
| Page has exhibitor names but no descriptions | Lazy-load on scroll | Playwright `scrollBy` loop |
| Site says "you need JS" but full text is present in HTML | Informa Sitefinity misleading banner | Just parse the HTML — ignore the banner |
| "Exhibitor List 2025" shown on 2026 event page | Prior-year content, 2026 not published yet | Document in notes, don't scrape 2025 as 2026 |
| Company names all show as `None` after dedupe | Agent put name in `Name` column, `Company` blank | Fall back to `Name` when `Company` is empty |
| 800+ rows all "Sponsor/Exhibitor" type | Non-normalized type value | Step 7 normalization map |
| Master shows 0 exhibitors for a conference that has 700 | CSV row corruption from naive comma-split | Use `Import-Csv`, not manual split |
| Playwright times out on `waitUntil: 'networkidle'` | Site keeps loading analytics forever | Use `waitUntil: 'domcontentloaded'` + explicit `waitForTimeout` |
| Windows `curl` writes URL fragment to filesystem | Git Bash `MSYS_NO_PATHCONV` conversion | Prefix command with `MSYS_NO_PATHCONV=1` or use Windows-style `C:\...` paths |

---

## Step 11 — Order of operations checklist per conference

1. [ ] Create output folder
2. [ ] Fetch homepage with Step 3 headers
3. [ ] Detect platform from HTML signatures (Step 2)
4. [ ] If known platform → run its fast-path recipe (Step 4A-G)
5. [ ] If unknown → header-menu-hunt for `/exhibitors`, `/sponsors`, `/attendees`, `/partners`, `/who-is-*`, `/exhibit-hall`, `/expo-floor` (Step 5A)
6. [ ] For each sub-page: fetch, extract with the 4-signal cascade (Step 5B)
7. [ ] If content is JS-blocked or capped → Playwright (Step 5C)
8. [ ] Clean, filter, dedupe (Step 6)
9. [ ] Write `exhibitors_speakers.csv` with canonical header
10. [ ] Split into 4 typed files (Step 7)
11. [ ] Write `notes.txt` documenting technique + gaps (Step 9)
12. [ ] Rebuild master `00_MASTER_all_companies.csv` (Step 8)

---

## Appendix — quick command snippets

**Test a site's block level:**

```bash
# Plain UA — most sites 403
curl -sI -A "Mozilla/5.0" "https://target.com/exhibitors"

# Full browser headers — usually works
curl -sI -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36" \
  -H "Sec-Fetch-Dest: document" -H "Sec-Fetch-Mode: navigate" -H "Sec-Fetch-Site: none" \
  "https://target.com/exhibitors"
```

**Find all header-menu candidate URLs:**

```python
import re, html
with open('homepage.html') as f: s = f.read()
links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{0,80})</a>', s)
for href, text in links:
    if re.search(r'exhibit|sponsor|attend|partner|floor|expo|companies', href + ' ' + text, re.I):
        print(f"{href[:100]} | {html.unescape(text).strip()[:60]}")
```

**Install Playwright fresh:**

```bash
mkdir pw && cd pw
npm init -y
npm install playwright
npx playwright install chromium
```

**Playwright API-response capture template:**

```js
page.on('response', async res => {
  const u = res.url();
  const ct = res.headers()['content-type'] || '';
  if ((u.includes('exhibit')||u.includes('sponsor')||u.includes('/api/')) && ct.includes('json')) {
    try {
      const body = await res.text();
      if (body.length > 500) fs.writeFileSync(`api_${Date.now()}.json`, body);
    } catch(e){}
  }
});
```
