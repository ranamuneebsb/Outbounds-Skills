"""
OnlineJobs.ph Fast Scraper — CORRECT VERSION
=============================================
Lessons learned and fixes applied:

1. DATE FILTER IN PHASE 1: Stop collecting per keyword when date < CUTOFF.
   The listing page already has data-temp with the date — no need to scrape
   the detail page to find the date. This cuts stubs from 20k to ~1k.

2. EMAIL FROM DESCRIPTION ONLY: Never scan full HTML. When logged in, the
   navbar contains our own login email — scanning full HTML picks it up.
   Only extract from id="job-description" section.

3. BLOCK LOGIN EMAIL: Explicitly exclude the runner's own account email
   (derived from the login email) from results.

4. SAFE CONCURRENCY: 100 parallel (not 400). The site rate-limits aggressively.
   At 400 concurrent, 99%+ requests get 429'd silently → 259 jobs scraped.
   At 100 concurrent, requests succeed → 10k+ jobs scraped.

5. RETRY ON 429: Exponential backoff instead of silently dropping.

6. SAVE STUBS TO DISK: After Phase 1, save stubs.json.

7. NO BATCH LOOP: Launch all tasks at once, let semaphore control concurrency.
   batch_size=50 was the bug that made 400 semaphore useless.

Target: <30 minutes total for fresh jobs only.

────────────────────────────────────────────────────────────────
CREDENTIALS: This script does NOT store any login. Each runner supplies
their own OnlineJobs.ph account at runtime (prompted), or via env vars
OJ_EMAIL / OJ_PASSWORD. The cutoff date is prompted too (or OJ_CUTOFF).
────────────────────────────────────────────────────────────────
"""

import asyncio
import getpass
import json
import os
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
import pandas as pd
from tqdm import tqdm

# ── CREDENTIALS (filled in at runtime — never hardcode a real login here) ──
EMAIL    = ""
PASSWORD = ""

# ── DATE FILTER (default; overridden at runtime by prompt or OJ_CUTOFF) ────
CUTOFF_DATE = date(2026, 7, 11)  # only jobs on/after this date are kept

# ── CONFIG ───────────────────────────────────────────────────
BASE_URL   = "https://www.onlinejobs.ph"
CONCURRENCY_LISTING = 170   # All keywords in parallel
CONCURRENCY_DETAIL  = 100   # Safe — avoids rate limiting
DELAY      = 0.3
OUTPUT_DIR = Path(__file__).parent
CHECKPOINT_DIR = OUTPUT_DIR / "oj_checkpoints_fast"
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

KEYWORDS = [
    # ── VA / Admin ──────────────────────────────────────────
    "virtual assistant", "VA", "executive assistant", "admin assistant",
    "personal assistant", "administrative assistant", "office assistant",
    "general virtual assistant", "remote assistant", "virtual staff",

    # ── Customer Service ────────────────────────────────────
    "customer service", "customer support", "chat support",
    "email support", "help desk", "technical support", "customer care",
    "live chat", "after sales support",

    # ── Sales / Outreach ────────────────────────────────────
    "appointment setter", "cold caller", "lead generation",
    "outreach specialist", "sales representative", "telemarketer",
    "business development", "SDR", "inside sales", "sales assistant",
    "cold email", "sales closer",

    # ── Marketing ───────────────────────────────────────────
    "social media", "social media manager", "social media marketing",
    "content creator", "content writer", "copywriter",
    "email marketing", "digital marketing", "SEO",
    "Google Ads", "Facebook Ads", "Meta Ads", "PPC",
    "marketing assistant", "brand assistant", "influencer marketing",
    "affiliate marketing", "link building", "content marketing",
    "TikTok", "Instagram", "YouTube", "Pinterest",
    "Klaviyo", "Mailchimp", "ActiveCampaign",

    # ── Finance / Accounting ────────────────────────────────
    "bookkeeping", "accounting", "QuickBooks", "Xero",
    "data entry", "payroll", "invoicing",
    "accounts receivable", "accounts payable",
    "financial assistant", "tax preparation", "Wave accounting",
    "FreshBooks", "Sage",

    # ── E-commerce ──────────────────────────────────────────
    "Amazon", "Shopify", "eBay", "Etsy", "dropshipping",
    "ecommerce", "product listing", "product research",
    "Amazon FBA", "Walmart seller", "Amazon PPC",
    "WooCommerce", "Magento", "BigCommerce",

    # ── Design / Creative ───────────────────────────────────
    "graphic design", "video editing", "Canva", "Photoshop",
    "Illustrator", "web design", "WordPress",
    "motion graphics", "thumbnail designer", "logo design",
    "After Effects", "Premiere Pro", "Figma", "UI UX",

    # ── Tech / Dev ──────────────────────────────────────────
    "web developer", "programmer", "Python", "automation",
    "Zapier", "n8n", "web scraping", "React", "JavaScript",
    "API integration", "Make.com", "Integromat",

    # ── Real Estate ─────────────────────────────────────────
    "real estate", "real estate assistant",
    "transaction coordinator", "property management",
    "real estate cold calling", "MLS", "real estate VA",

    # ── Project / Operations ────────────────────────────────
    "project manager", "operations manager", "operations assistant",
    "Trello", "Asana", "ClickUp", "Monday", "Notion",
    "process improvement", "SOPs",

    # ── CRM / Tools ─────────────────────────────────────────
    "GoHighLevel", "GHL", "HubSpot", "Salesforce",
    "Zoho", "Pipedrive", "Zendesk", "Freshdesk",
    "CRM", "Airtable", "Slack", "Intercom",

    # ── HR / Recruiting ─────────────────────────────────────
    "recruiter", "HR assistant", "hiring", "resume",
    "recruitment", "sourcing", "LinkedIn recruiter",
    "talent acquisition",

    # ── Content / Media ─────────────────────────────────────
    "video editor", "podcast", "blog writer",
    "article writer", "proofreading", "transcription",
    "research", "web research", "lead researcher",

    # ── Finance / Investment ────────────────────────────────
    "forex", "crypto", "stock market", "trading assistant",

    # ── Healthcare / Niche ──────────────────────────────────
    "medical transcription", "medical billing",
    "dental assistant", "healthcare VA",

    # ── General catch-all ───────────────────────────────────
    "remote work", "work from home", "online job",
    "part time", "full time remote",
]

COLUMNS = [
    "job_id", "title", "job_category", "type_of_work", "salary", "hours_per_week",
    "date_updated", "company_name", "company_website", "email",
    "skills", "description", "job_url", "keywords_matched",
]

JOB_CATEGORIES = [
    ("Virtual Assistant / Admin",   r"virtual assistant|exec(utive)? assistant|admin|personal assistant|\bVA\b|administrative|secretary"),
    ("Customer Service / Support",  r"customer service|customer support|chat support|help desk|technical support|customer care|live chat|after sales"),
    ("Sales / Outreach",            r"appointment setter|cold call|lead gen|outreach|sales rep|telemarket|\bSDR\b|inside sales|sales closer|sales assistant|business dev|account manager"),
    ("Social Media / Marketing",    r"social media|instagram|tiktok|youtube|pinterest|facebook ads|google ads|meta ads|\bPPC\b|email marketing|digital marketing|influencer|affiliate|klaviyo|mailchimp|media buy"),
    ("SEO / Content Writing",       r"\bSEO\b|copywriter|content writer|blog writer|article writer|proofreading|transcription|content creator|ghostwriter|\beditor\b"),
    ("Design / Video / Creative",   r"graphic design|video edit|canva|photoshop|illustrator|figma|UI.?UX|web design|motion graphics|thumbnail|logo design|after effects|premiere|designer|animator"),
    ("Tech / Dev / Automation",     r"web dev|developer|programmer|python|javascript|react|\bAPI\b|zapier|n8n|make\.com|automation|web scraping|wordpress|software|engineer|\bPHP\b"),
    ("E-commerce / Amazon",         r"amazon|shopify|ebay|etsy|dropship|ecommerce|product listing|product research|\bFBA\b|walmart|woocommerce|bigcommerce|magento|funnel|inventory"),
    ("Finance / Accounting",        r"bookkeep|accounting|quickbooks|xero|payroll|invoic|accounts receivable|accounts payable|tax prep|financial|freshbooks|\bsage\b|billing|\bCPA\b"),
    ("Real Estate",                 r"real estate|transaction coordinator|property manag|\bMLS\b|property"),
    ("Project / Operations",        r"project manag|operations manag|trello|asana|clickup|monday\.com|notion|process improve|\bSOP\b|coordinator"),
    ("CRM / Tools",                 r"gohighlevel|\bGHL\b|hubspot|salesforce|zoho|pipedrive|zendesk|freshdesk|\bCRM\b|airtable|intercom"),
    ("HR / Recruiting",             r"recruit|HR assist|talent acqui|sourcing|linkedin recruit|hiring|resume screen|human resource"),
    ("Healthcare",                  r"medical transcri|medical billing|dental|healthcare VA|health|nurse|clinical"),
    ("Finance / Investment",        r"forex|crypto|trading|stock market|investment"),
    ("Research / Data",             r"\bresearch\b|data entry|data analyst|lead research|web research|market research"),
]

def parse_date(date_str: str):
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except:
            continue
    return None

def is_fresh(date_str: str) -> bool:
    d = parse_date(date_str)
    return d is not None and d >= CUTOFF_DATE

def title_from_url(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d{5,}$", "", slug)
    return slug.replace("-", " ").strip()

def categorize_title(title: str) -> str:
    t = title.lower()
    for cat, pattern in JOB_CATEGORIES:
        if re.search(pattern, t, re.I):
            return cat
    return "Other / General"

def clean(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def extract_email(desc_text: str) -> str:
    if not desc_text:
        return ""
    emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", desc_text)
    # Block generic non-employer emails and the runner's own login email
    blocklist = {
        "onlinejobs", "test", "example", "dummy", "placeholder", "noreply",
    }
    login_local = EMAIL.split("@")[0].lower() if EMAIL else ""
    if login_local:
        blocklist.add(login_local)
    real = [
        e for e in emails
        if not any(b in e.lower() for b in blocklist)
        and len(e) > 6
        and e.count("@") == 1
    ]
    business = [e for e in real if "gmail" not in e.lower()]
    return business[0] if business else (real[0] if real else "")

def extract_company(text: str) -> str:
    m = re.search(r"About\s+Us[\s\W]+([A-Z][^\n.]{3,60}?)\s+(?:is|are|was)\s+a", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:^|\n)About\s+([A-Z][A-Za-z0-9 &,.\-]{3,50})(?:\n|:)", text)
    if m:
        name = m.group(1).strip()
        if name.lower() not in ("us", "the role", "the company", "the position"):
            return name
    m = re.search(r"([A-Z][A-Za-z0-9 &,.\-]{3,50})\s+is\s+a[n]?\s+(?:fast-growing|US-based|growing|remote|leading)", text)
    if m:
        return m.group(1).strip()
    return ""

_NON_COMPANY_DOMAINS = {
    "youtube.com","youtu.be","instagram.com","facebook.com","fb.com",
    "tiktok.com","twitter.com","x.com","pinterest.com","reddit.com",
    "linkedin.com","snapchat.com","loom.com","vimeo.com","vocaroo.com",
    "speedtest.net","jotform.com","surveymonkey.com","typeform.com",
    "airtable.com","forms.gle","docs.google.com","drive.google.com",
    "notion.so","canva.com","dropbox.com","trello.com","asana.com",
    "clickup.com","monday.com","slack.com","onlinejobs.ph","indeed.com",
    "upwork.com","freelancer.com","zoom.us","calendly.com",
    "wikipedia.org","amazon.com","google.com",
}

def extract_website(text: str) -> str:
    urls = re.findall(r"(?:https?://|www\.)[a-zA-Z0-9][a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s<>\"']*)?", text)
    for url in urls:
        domain = re.sub(r"^https?://", "", url)
        domain = re.sub(r"^www\.", "", domain)
        domain = domain.split("/")[0].lower()
        if domain in _NON_COMPANY_DOMAINS:
            continue
        if any(domain.endswith("." + d) for d in _NON_COMPANY_DOMAINS):
            continue
        return url.rstrip(".,)")
    return ""

# ── RUNTIME CONFIG: prompt for login + cutoff (env vars override) ─────
def load_runtime_config():
    global EMAIL, PASSWORD, CUTOFF_DATE

    EMAIL = (os.environ.get("OJ_EMAIL") or "").strip()
    if not EMAIL:
        EMAIL = input("OnlineJobs.ph email: ").strip()

    PASSWORD = os.environ.get("OJ_PASSWORD") or ""
    if not PASSWORD:
        PASSWORD = getpass.getpass("OnlineJobs.ph password (hidden): ").strip()

    raw = (os.environ.get("OJ_CUTOFF") or "").strip()
    if not raw:
        raw = input(f"Cutoff date YYYY-MM-DD [{CUTOFF_DATE}]: ").strip()
    if raw:
        try:
            CUTOFF_DATE = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print(f"[WARN] Could not parse '{raw}', using default {CUTOFF_DATE}")

    if not EMAIL or not PASSWORD:
        print("[ERROR] Email and password are required. Aborting.")
        sys.exit(1)

# ── LOGIN ─────────────────────────────────────────────────────
async def login(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.get(f"{BASE_URL}/login", timeout=30)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
        token = m.group(1) if m else ""
        r2 = await client.post(f"{BASE_URL}/authenticate", data={
            "csrf-token": token,
            "info[email]": EMAIL,
            "info[password]": PASSWORD,
        }, timeout=30)
        if "logout" in r2.text.lower() or "myaccount" in r2.text.lower():
            print("[OK] Logged in")
            return True
        print("[FAIL] Login failed — check your email/password")
        return False
    except Exception as e:
        print(f"[ERROR] Login: {e}")
        return False

# ── PHASE 1: collect stubs — stop per keyword when date < CUTOFF ──
async def fetch_listing_page(client, sem, url):
    async with sem:
        try:
            r = await client.get(url, timeout=20)
            if r.status_code != 200:
                return []
        except Exception:
            return []

    stubs = []
    for m in re.finditer(r'href="/jobseekers/job/([^"]+)"', r.text):
        path = m.group(1)
        id_m = re.search(r"-(\d+)$", path)
        if not id_m:
            continue
        job_id = id_m.group(1)
        date_m = re.search(r'data-temp="([^"]+)"', r.text[m.start():m.start() + 600])
        date_str = date_m.group(1) if date_m else ""
        stubs.append({
            "job_id":   job_id,
            "job_url":  f"{BASE_URL}/jobseekers/job/{path}",
            "date_str": date_str,
        })
    return stubs


async def collect_stubs_for_keyword(client, sem, kw, seen_ids):
    encoded = re.sub(r"\s+", "+", kw.strip())
    new_stubs = []

    for page_num in range(0, 10):  # max 10 pages per keyword
        offset = page_num * 30
        if offset == 0:
            url = f"{BASE_URL}/jobseekers/jobsearch?jobkeyword={encoded}"
        else:
            url = f"{BASE_URL}/jobseekers/jobsearch/{offset}?jobkeyword={encoded}"

        page_stubs = await fetch_listing_page(client, sem, url)
        if not page_stubs:
            break

        all_old = True
        for stub in page_stubs:
            if is_fresh(stub["date_str"]):
                all_old = False
                if stub["job_id"] not in seen_ids:
                    seen_ids[stub["job_id"]] = True
                    stub["keyword"] = kw
                    new_stubs.append(stub)

        # Early exit: entire page is old jobs — stop paginating this keyword
        if all_old:
            break

    return new_stubs


async def collect_all_stubs(client, seen_ids):
    sem = asyncio.Semaphore(CONCURRENCY_LISTING)
    tasks = [collect_stubs_for_keyword(client, sem, kw, seen_ids) for kw in KEYWORDS]
    results = await asyncio.gather(*tasks)
    all_stubs = []
    for kw_stubs in results:
        all_stubs.extend(kw_stubs)
    return all_stubs


# ── PHASE 2: scrape detail — description only for email ───────
async def scrape_detail(client, sem, stub, all_jobs):
    jid  = stub["job_id"]
    url  = stub["job_url"]

    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url, timeout=20)
                if r.status_code in (404, 410, 403, 401):
                    return
                if r.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if r.status_code != 200:
                    return
                html = r.text
                break
            except Exception:
                if attempt == 2:
                    return
                await asyncio.sleep(1)
        else:
            return

    row = {
        "job_id":          jid,
        "job_url":         url,
        "keywords_matched": stub.get("keyword", ""),
        "title":           title_from_url(url),
        "job_category":    "",
        "type_of_work":    "",
        "salary":          "",
        "hours_per_week":  "",
        "date_updated":    stub.get("date_str", ""),
        "skills":          "",
        "email":           "",
        "company_name":    "",
        "company_website": "",
        "description":     "",
    }

    # Title
    m = re.search(r'class="job__title"[^>]*>([^<]+)<', html)
    if m:
        row["title"] = clean(m.group(1)) or row["title"]

    # Structured fields
    for dl in re.finditer(r"(?s)<dl[^>]*class=\"row no-gutters[^\"]*\"[^>]*>(.*?)</dl>", html):
        dl_text = dl.group(1)
        h3 = re.search(r"<h3[^>]*>([^<]+)</h3>", dl_text)
        p  = re.search(r'<p[^>]*class="fs-18"[^>]*>([^<]+)</p>', dl_text)
        if not h3 or not p:
            continue
        label = h3.group(1).upper()
        value = clean(p.group(1))
        if   "TYPE"  in label: row["type_of_work"]  = value
        elif "WAGE"  in label or "SALARY" in label: row["salary"] = value
        elif "HOURS" in label: row["hours_per_week"] = value
        elif "DATE"  in label: row["date_updated"]   = value or row["date_updated"]

    # Skills
    skills = re.findall(r'class="card-worker-topskill"[^>]*>([^<]+)<', html)
    row["skills"] = ", ".join(clean(s) for s in skills if clean(s))

    # Description — strip ojfilter, extract from description section ONLY
    desc_text = ""
    desc_m = re.search(r'(?s)id="job-description"[^>]*>(.*?)</div>', html)
    if desc_m:
        desc_html = desc_m.group(1)
        desc_html = re.sub(r"<ojfilter[^>]*>.*?</ojfilter>", " ", desc_html, flags=re.S)
        desc_text = clean(desc_html)
        row["description"] = desc_text

    # Email: ONLY from description — never from full page
    row["email"]           = extract_email(desc_text)
    row["company_name"]    = extract_company(desc_text)
    row["company_website"] = extract_website(desc_text)

    # Date fallback
    if not row["date_updated"]:
        dm = re.search(r'data-temp="([^"]+)"', html)
        if dm:
            row["date_updated"] = dm.group(1)

    row["job_category"] = categorize_title(row["title"])
    all_jobs[jid] = row


# ── MAIN ─────────────────────────────────────────────────────
async def main():
    t_start = time.time()
    print("=" * 60)
    print("OnlineJobs.ph FAST Scraper — Fresh Jobs")
    print(f"Cutoff date: {CUTOFF_DATE} (keeping jobs on/after this date)")
    print("=" * 60)

    seen_ids: dict = {}
    all_jobs: dict = {}

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30,
        verify=False,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
    ) as client:

        ok = await login(client)
        if not ok:
            print("Aborting.")
            return

        # Phase 1
        print(f"\nPHASE 1: Collecting stubs on/after {CUTOFF_DATE} ({len(KEYWORDS)} keywords in parallel)...")
        t1 = time.time()
        stubs = await collect_all_stubs(client, seen_ids)
        print(f"Phase 1 done in {time.time()-t1:.0f}s")
        print(f"  Fresh stubs (on/after {CUTOFF_DATE}): {len(stubs)}")

        if not stubs:
            print("No fresh jobs found.")
            return

        # Save stubs
        stubs_file = OUTPUT_DIR / "fast_stubs.json"
        with open(stubs_file, "w") as f:
            json.dump(stubs, f)

        # Phase 2
        print(f"\nPHASE 2: Scraping {len(stubs)} fresh job detail pages...")
        print(f"  Concurrency: {CONCURRENCY_DETAIL} | Email: description-only")
        t2 = time.time()
        sem_detail = asyncio.Semaphore(CONCURRENCY_DETAIL)

        async def scrape_and_update(stub, pbar):
            await scrape_detail(client, sem_detail, stub, all_jobs)
            pbar.update(1)

        with tqdm(total=len(stubs), unit="job", colour="green") as pbar:
            await asyncio.gather(*[scrape_and_update(s, pbar) for s in stubs])

        print(f"Phase 2 done in {time.time()-t2:.0f}s")

    # Save
    print(f"\nSaving results...")
    df = pd.DataFrame(all_jobs.values())
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[COLUMNS].drop_duplicates(subset=["job_id"])
    df["date_updated"] = pd.to_datetime(df["date_updated"], errors="coerce")
    df = df.sort_values("date_updated", ascending=False)
    df["date_updated"] = df["date_updated"].astype(str)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = OUTPUT_DIR / f"jobs_from_{CUTOFF_DATE}_{ts}.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    total  = len(df)
    emails = (df["email"].str.strip() != "").sum()
    elapsed = time.time() - t_start

    print(f"\n{'='*60}")
    print(f"DONE in {elapsed/60:.1f} minutes")
    print(f"{'='*60}")
    print(f"  Total jobs (on/after {CUTOFF_DATE}) : {total}")
    print(f"  With emails                         : {emails} ({emails*100//max(total,1)}%)")
    print(f"  Saved: {out_csv.name}")

    # Log
    with open(OUTPUT_DIR / "process_log.md", "a", encoding="utf-8") as log:
        log.write(
            f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"- **Source**: onlinejobs.ph (jobs on/after {CUTOFF_DATE})\n"
            f"- **Keywords**: {len(KEYWORDS)}\n"
            f"- **Total jobs**: {total}\n"
            f"- **With email**: {emails}\n"
            f"- **Duration**: {elapsed/60:.1f} min\n"
            f"- **Output**: `{out_csv.name}`\n"
        )

if __name__ == "__main__":
    load_runtime_config()
    asyncio.run(main())
