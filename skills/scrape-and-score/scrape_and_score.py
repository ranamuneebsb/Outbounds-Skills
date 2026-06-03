#!/usr/bin/env python3
"""
scrape-and-score — merged ZenRows scraper + ICP classifier + company-scoring.

Description-first hybrid pipeline. For every company row in a CSV:

  Step 1  Read the description column.
  Step 2  Screen it with a cheap LLM (gpt-4o-mini) that returns a
          `needs_scrape` flag alongside a tentative score.
  Step 3  Is the description clear enough to score?
            YES -> score directly from the description. No scraping. Cheap+fast.
            NO  -> (missing / thin / vague / ambiguous, OR the company name has
                   ICP keywords but the description contradicts them) trigger the
                   scrape ladder, then score the scraped text with a stronger model:

            Phase 0  httpx + html2text         (FREE)  async GET, HTML->clean text,
                                                        HTML capped at 500 KB.
            Phase 1  ZenRows basic              (paid)  autoparse=true, antibot=false.
                                                        Only if httpx fails / thin.
            Phase 2  ZenRows antibot + JS render (paid)  autoparse=true, antibot=true,
                                                        js_render=true. Final attempt;
                                                        if it fails -> scrape_failed.

Scoring logic is taken verbatim from Bryan's company-scoring skill (same prompts,
same 1-10 rubric, same borderline escalation):
  - gpt-4o-mini                      description screening
  - deepseek/deepseek-chat-v3.1      scrape scoring
  - anthropic/claude-sonnet-4-6      borderline escalation (score 5-7, weak confidence)

Speed: async scraping (httpx.AsyncClient, conc 50 free / 30 ZenRows), LLM calls on
a 30-worker thread pool, rows scored the moment they're scraped, results streamed
to CSV, SQLite scrape cache for restart, tqdm live progress.

API KEYS ARE ALWAYS PASSED ON THE COMMAND LINE. The script never reads keys from
the environment, from ~/.olympolabs/, or from any saved-credential file.

    python scrape_and_score.py "input.csv" \
      --icp "icp-profile.md" \
      --zenrows-key YOUR_ZENROWS_KEY \
      --openrouter-key YOUR_OPENROUTER_KEY \
      --concurrency 50 \
      --score-threshold 7
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---- Dependencies (auto-install) ----

def _pip_install(pkg: str) -> None:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])


try:
    import httpx
except ImportError:
    _pip_install("httpx")
    import httpx

try:
    import html2text
except ImportError:
    _pip_install("html2text")
    import html2text

try:
    import requests
except ImportError:
    _pip_install("requests")
    import requests

try:
    from tqdm import tqdm
except ImportError:
    _pip_install("tqdm")
    from tqdm import tqdm

# Long homepage_content fields blow past the default 131072-char csv cap.
csv.field_size_limit(10**9)


# ================================================================
# Constants
# ================================================================

DEFAULT_DESCRIPTION_MODEL = "openai/gpt-4o-mini"
DEFAULT_SCRAPE_MODEL = "deepseek/deepseek-chat-v3.1"
DEFAULT_ESCALATION_MODEL = "anthropic/claude-sonnet-4-6"

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"

# Speed / fast-fail settings (see spec).
HTTPX_TIMEOUT = 8.0           # free Tier-0 fetch: fail fast
ZENROWS_TIMEOUT = 12.0        # paid fallback
HTTPX_CONCURRENCY = 50        # default; overridable via --concurrency
ZENROWS_CONCURRENCY = 30      # paid, semaphore-capped
SCORE_WORKERS = 30            # LLM thread-pool workers
MAX_RETRIES = 1               # 1 retry only on scrape network errors

MAX_HTML_BYTES = 500 * 1024   # cap HTML before html2text (CPU-spin guard)
MIN_TEXT_LEN = 200            # min cleaned-text chars to accept a scrape
SCRAPE_TEXT_CAP = 20000       # cap cleaned text fed to the LLM / stored

ESCALATION_SCORE_LOWER = 5
ESCALATION_SCORE_UPPER = 7

# Approximate OpenRouter pricing, USD per 1M tokens (input, output).
PRICING = {
    "openai/gpt-4o-mini": (0.15, 0.60),
    "deepseek/deepseek-chat-v3.1": (0.30, 1.20),
    "anthropic/claude-sonnet-4-6": (3.00, 15.00),
}
_PRICING_FALLBACK = (1.00, 3.00)

# Rough ZenRows per-request credit cost (USD), for the cost estimate only.
ZENROWS_COST_BASIC = 0.002
ZENROWS_COST_JS = 0.005

NEW_COLUMNS = [
    "homepage_content", "homepage_status", "scrape_phase",
    "ICP Score", "ICP Verdict", "Scoring Source", "ICP Reasoning",
]

WEBSITE_KEYS = ["website", "domain", "url", "site", "homepage", "web"]
DESCRIPTION_KEYS = ["description", "desc", "about", "summary", "overview", "bio"]
COMPANY_KEYS = ["company", "name", "firm", "organization", "org", "business"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


# ================================================================
# Prompts — copied verbatim from Bryan's company-scoring prompts.py
# ================================================================

SCORING_RUBRIC = """\
Score this company on a 1-10 scale for how well it matches the ICP:

  10 = Perfect, textbook fit. The ICP describes this company exactly.
  8-9 = Strong fit. Maybe one small variable off (geography, size band) but
        clearly the right kind of company.
  7   = Decent fit, qualifies (>= 7 means we email them). Some elements match,
        nothing disqualifying.
  4-6 = Unclear or partial fit. Some signals match the ICP, others don't, or
        you can't tell for sure.
  1-3 = Clearly outside ICP. Wrong industry, wrong segment, or insufficient
        company-like signal.

Confidence levels:
  "high"   = You can clearly tell what this company does and you're confident
             the score is right.
  "medium" = Reasonable inference but the data is thin or ambiguous in places.
  "low"    = You're guessing. The description/page is too vague, generic, or
             could plausibly support several different interpretations.

Verdict mapping:
  score >= 7 -> "Qualified"
  score <  7 -> "Disqualified"
"""

DESCRIPTION_SYSTEM_TEMPLATE = """\
You are an ICP-fit classifier for OlympoLabs, a cold-outbound agency. Your
job is to read a short company description and decide how well the company
matches the ICP defined below, then return a JSON object with the score,
confidence, verdict, a needs_scrape flag, and a one-line reasoning.

# ICP definition

{icp_definition}

# Scoring rubric

{rubric}

# When to set needs_scrape

If the description is so thin, vague, or generic that you cannot make a
confident score, set "needs_scrape": true. The orchestrator will then scrape
the company website and ask you again with more context. Use this when:
  - Description is fewer than ~15 words AND doesn't clearly name the industry
  - Description is a generic placeholder ("Software company", "Consulting firm")
  - Description could plausibly be the right ICP or the wrong one - genuine
    uncertainty, not just slight ambiguity

ALSO set "needs_scrape": true (instead of disqualifying) when BOTH hold:
  - The company NAME or description contains strong positive ICP signals
    (industry keywords the ICP lists as fits), AND
  - You cannot confirm from the description ALONE that the company meets the
    ICP's core-business test, OR it resembles one of the nuanced edge cases the
    ICP explicitly calls out (variants, hybrids, contract/outsourced models,
    look-alike names, or any "fits unless..." caveat).
The principle: when a company that plausibly QUALIFIES would otherwise be
disqualified only because the description is ambiguous about the core test,
scrape to verify rather than guessing a disqualify. Borderline qualified
leads are expensive to lose; a scrape is cheap.

HARD RULE: if the company NAME itself contains one of the ICP's core industry
keywords (the words that define a fit), but the description would lead you to a
disqualify (score < 7), do NOT disqualify from the description alone - set
"needs_scrape": true. A company whose very name matches the ICP but whose
description reads like a brand portfolio, holding company, or something off is
the single highest-value case to verify by scraping. (A name that merely
mentions a keyword in passing without being that kind of company does not
count - judge whether the name asserts the company IS an ICP-type business.)

Do NOT set needs_scrape for companies that clearly belong to an unrelated
industry with no ICP keywords in the name or description - score those
directly as a disqualify.

If you CAN score with at least medium confidence from the description alone
(and none of the above apply), set "needs_scrape": false even when confidence
isn't "high". The orchestrator will independently decide whether to escalate
based on score + confidence.

# Output format

Return EXACTLY this JSON shape, with no commentary, no markdown, no preamble:

{{
  "score": <integer 1-10>,
  "confidence": "<high|medium|low>",
  "verdict": "<Qualified|Disqualified>",
  "needs_scrape": <true|false>,
  "reasoning": "<one short sentence, max 80 chars, why this score>"
}}
"""

SCRAPE_SYSTEM_TEMPLATE = """\
You are an ICP-fit classifier for OlympoLabs, a cold-outbound agency. Your
job is to read extracted text from a company's website and decide how well
the company matches the ICP defined below, then return a JSON object with the
score, confidence, verdict, and a one-line reasoning.

The text below was extracted from the company's homepage HTML; nav, footer,
scripts, and other boilerplate have largely been stripped.

# ICP definition

{icp_definition}

# Scoring rubric

{rubric}

# Output format

Return EXACTLY this JSON shape, with no commentary, no markdown, no preamble:

{{
  "score": <integer 1-10>,
  "confidence": "<high|medium|low>",
  "verdict": "<Qualified|Disqualified>",
  "reasoning": "<one short sentence, max 80 chars, why this score>"
}}

If the scraped text is empty, garbage, or clearly an error page (e.g.
"This domain is for sale", a parked page, a Cloudflare challenge), score 1
with confidence high and reasoning "no usable site content".
"""


def build_description_system(icp: str) -> str:
    return DESCRIPTION_SYSTEM_TEMPLATE.format(icp_definition=icp.strip(), rubric=SCORING_RUBRIC)


def build_scrape_system(icp: str) -> str:
    return SCRAPE_SYSTEM_TEMPLATE.format(icp_definition=icp.strip(), rubric=SCORING_RUBRIC)


def description_user_prompt(name: str, domain: str, description: str) -> str:
    return (
        f"Company name: {name or '(unknown)'}\n"
        f"Domain: {domain or '(unknown)'}\n"
        f"Description:\n{description}\n"
    )


def scrape_user_prompt(name: str, domain: str, scraped_text: str) -> str:
    return (
        f"Company name: {name or '(unknown)'}\n"
        f"Domain: {domain or '(unknown)'}\n"
        f"Extracted website text:\n---\n{scraped_text}\n---\n"
    )


# ================================================================
# Helpers
# ================================================================

def detect_column(headers: List[str], keys: List[str]) -> Optional[str]:
    lower = [(h, h.lower()) for h in headers]
    for key in keys:
        for orig, low in lower:
            if key in low:
                return orig
    return None


def normalize_url(domain_or_url: str) -> Optional[str]:
    if not domain_or_url:
        return None
    s = str(domain_or_url).strip()
    if not s:
        return None
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    return s


def is_blank(cell: Any) -> bool:
    return cell is None or (isinstance(cell, str) and not cell.strip())


def safe_int_score(value: Any) -> Optional[int]:
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 10 else None


def clean_html(html: str) -> str:
    """HTML -> clean readable text via html2text. HTML is capped at 500 KB
    before conversion to stop html2text spinning on huge pages."""
    if not html:
        return ""
    if len(html) > MAX_HTML_BYTES:
        html = html[:MAX_HTML_BYTES]
    try:
        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        h.ignore_emphasis = True
        h.body_width = 0
        text = h.handle(html)
    except Exception:
        return html[:SCRAPE_TEXT_CAP].strip()
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:SCRAPE_TEXT_CAP]


def parse_json_content(text: str) -> Dict[str, Any]:
    """Parse an LLM JSON reply, tolerating ```json fences and surrounding prose."""
    if not text:
        raise ValueError("empty LLM response")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        first, last = s.find("{"), s.rfind("}")
        if first != -1 and last != -1 and last > first:
            return json.loads(s[first:last + 1])
        raise


def model_cost(model: str, in_tok: int, out_tok: int) -> float:
    pin, pout = PRICING.get(model, _PRICING_FALLBACK)
    return in_tok / 1_000_000 * pin + out_tok / 1_000_000 * pout


def load_icp(icp_arg: str) -> Tuple[str, str]:
    """--icp is a path to a markdown/txt file, or inline ICP text."""
    p = Path(icp_arg).expanduser()
    if p.exists() and p.is_file():
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"ICP file is empty: {p}")
        return p.stem, text
    if not icp_arg.strip():
        raise ValueError("ICP is empty")
    return "<inline>", icp_arg.strip()


# ================================================================
# Scrape cache (SQLite) — caches BOTH successes and failures
# ================================================================

class ScrapeCache:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS scrape_cache ("
            "url TEXT PRIMARY KEY, status TEXT, phase TEXT, content TEXT)"
        )
        self._conn.commit()
        self._urls = {u for (u,) in self._conn.execute("SELECT url FROM scrape_cache")}

    def __contains__(self, url: str) -> bool:
        return url in self._urls

    def get(self, url: str) -> Optional[Tuple[str, str, str]]:
        cur = self._conn.execute(
            "SELECT content, status, phase FROM scrape_cache WHERE url = ?", (url,)
        )
        row = cur.fetchone()
        return (row[0], row[1], row[2]) if row else None

    def put(self, url: str, content: str, status: str, phase: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO scrape_cache (url, status, phase, content) "
            "VALUES (?, ?, ?, ?)",
            (url, status, str(phase), content),
        )
        self._conn.commit()
        self._urls.add(url)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ================================================================
# Orchestrator
# ================================================================

class Runner:
    def __init__(self, args, icp_def: str):
        self.args = args
        self.desc_sys = build_description_system(icp_def)
        self.scrape_sys = build_scrape_system(icp_def)
        self.threshold = args.score_threshold
        self.escalate = not args.no_escalation
        self.use_zenrows = not args.no_zenrows

        # Concurrency primitives
        self.sem_httpx = asyncio.Semaphore(args.concurrency)
        self.sem_zr = asyncio.Semaphore(args.zenrows_concurrency)
        self.llm_exec = ThreadPoolExecutor(max_workers=args.score_workers)

        # Shared HTTP clients
        self.aclient: Optional[httpx.AsyncClient] = None
        self.or_session = requests.Session()
        self.or_session.headers.update({
            "Authorization": f"Bearer {args.openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/ranamuneebsb/Olympo-labs-skills",
            "X-Title": "scrape-and-score",
        })

        # Counters (mutated only on the event-loop thread)
        self.c = {
            "scraped": 0, "scored": 0, "qualified": 0, "errors": 0,
            "httpx_hits": 0, "zenrows_calls": 0,
            "zenrows_basic": 0, "zenrows_js": 0,
            "llm_cost": 0.0, "zenrows_cost": 0.0,
            "llm_calls": {},
        }

    # ---- LLM (sync, runs in thread pool) ----

    def _llm_chat_sync(self, model: str, system: str, user: str, max_tokens: int) -> Tuple[Dict[str, Any], float]:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        resp = self.or_session.post(OPENROUTER_ENDPOINT, json=body, timeout=90)
        if resp.status_code != 200:
            raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {}) or {}
        cost = model_cost(model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return parse_json_content(content), cost

    async def call_llm(self, model: str, system: str, user: str, max_tokens: int) -> Dict[str, Any]:
        loop = asyncio.get_event_loop()
        result, cost = await loop.run_in_executor(
            self.llm_exec, partial(self._llm_chat_sync, model, system, user, max_tokens)
        )
        self.c["llm_cost"] += cost
        self.c["llm_calls"][model] = self.c["llm_calls"].get(model, 0) + 1
        return result

    # ---- Scrape ladder (async) ----

    async def _httpx_fetch(self, url: str) -> str:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with self.sem_httpx:
            try:
                r = await self.aclient.get(url, headers=headers, timeout=HTTPX_TIMEOUT)
            except Exception:
                return ""
        if r.status_code != 200:
            return ""
        ctype = r.headers.get("content-type", "").lower()
        if "html" not in ctype and "text" not in ctype:
            return ""
        text = await asyncio.to_thread(clean_html, r.text)
        return text if len(text) >= MIN_TEXT_LEN else ""

    async def _zenrows_fetch(self, url: str, *, antibot: bool, js: bool) -> str:
        params = {"url": url, "apikey": self.args.zenrows_key, "autoparse": "true"}
        if antibot:
            params["antibot"] = "true"
        if js:
            params["js_render"] = "true"
        async with self.sem_zr:
            self.c["zenrows_calls"] += 1
            if js:
                self.c["zenrows_js"] += 1
                self.c["zenrows_cost"] += ZENROWS_COST_JS
            else:
                self.c["zenrows_basic"] += 1
                self.c["zenrows_cost"] += ZENROWS_COST_BASIC
            for attempt in range(MAX_RETRIES + 1):
                try:
                    r = await self.aclient.get(
                        ZENROWS_ENDPOINT, params=params, timeout=ZENROWS_TIMEOUT
                    )
                    if r.status_code == 200 and r.text:
                        text = await asyncio.to_thread(clean_html, r.text)
                        return text if len(text) >= MIN_TEXT_LEN else ""
                    return ""
                except Exception:
                    if attempt >= MAX_RETRIES:
                        return ""
        return ""

    async def scrape(self, url: Optional[str]) -> Tuple[str, str, str]:
        """Returns (content, status, phase). status in
        ok_httpx / ok_zenrows_basic / ok_zenrows_js / scrape_failed."""
        if not url:
            return "", "scrape_failed", ""

        cached = None
        if url in self._cache:
            cached = self._cache.get(url)
        if cached is not None:
            content, status, phase = cached
            return content, status, phase

        self.c["scraped"] += 1

        # Phase 0 — free httpx
        text = await self._httpx_fetch(url)
        if text:
            self.c["httpx_hits"] += 1
            self._cache.put(url, text, "ok_httpx", "0")
            return text, "ok_httpx", "0"

        if self.use_zenrows and self.args.zenrows_key:
            # Phase 1 — ZenRows basic (autoparse=true, antibot=false)
            text = await self._zenrows_fetch(url, antibot=False, js=False)
            if text:
                self._cache.put(url, text, "ok_zenrows_basic", "1")
                return text, "ok_zenrows_basic", "1"

            # Phase 2 — ZenRows antibot + JS render combined
            text = await self._zenrows_fetch(url, antibot=True, js=True)
            if text:
                self._cache.put(url, text, "ok_zenrows_js", "2")
                return text, "ok_zenrows_js", "2"

        self._cache.put(url, "", "scrape_failed", "")
        return "", "scrape_failed", ""

    # ---- Per-row scoring (the documented description-first flow) ----

    def _build(self, result: Dict[str, Any], source: str,
               content: str, status: str, phase: str) -> Dict[str, Any]:
        score = safe_int_score(result.get("score"))
        if score is None:
            return self._error(f"model returned invalid score: {result.get('score')!r}",
                               content, status, phase)
        verdict = "Qualified" if score >= self.threshold else "Disqualified"
        return {
            "score": score, "verdict": verdict, "source": source,
            "reasoning": str(result.get("reasoning", ""))[:200],
            "homepage_content": content, "homepage_status": status,
            "scrape_phase": phase, "error": None,
        }

    def _error(self, message: str, content="", status="", phase="") -> Dict[str, Any]:
        return {
            "score": "", "verdict": "Error", "source": "failed",
            "reasoning": message[:200], "homepage_content": content,
            "homepage_status": status, "scrape_phase": phase, "error": message,
        }

    async def score_row(self, name: str, domain: str, description: str) -> Dict[str, Any]:
        url = normalize_url(domain)

        # --scrape-only: skip the description path entirely.
        if self.args.scrape_only:
            return await self._scrape_then_score(name, domain, url)

        # Step 1+2: description screening with the cheap model.
        if description and description.strip():
            try:
                r = await self.call_llm(self.args.description_model, self.desc_sys,
                                        description_user_prompt(name, domain, description), 600)
            except Exception as exc:
                return self._error(f"description-mode LLM error: {exc}")

            if not bool(r.get("needs_scrape")):
                # Clear enough to score from the description — NO scraping...
                if self.args.description_only:
                    return self._build(r, "description", "", "", "")
                # ...unless it's a borderline 5-7 with weak confidence and
                # escalation is on (Bryan's logic: verify with a scrape + smart model).
                score = safe_int_score(r.get("score")) or 0
                conf = str(r.get("confidence", "")).lower()
                if self.escalate and ESCALATION_SCORE_LOWER <= score <= ESCALATION_SCORE_UPPER \
                        and conf in ("low", "medium") and url:
                    content, status, phase = await self.scrape(url)
                    if content:
                        try:
                            r2 = await self.call_llm(self.args.escalation_model, self.scrape_sys,
                                                     scrape_user_prompt(name, domain, content), 400)
                            return self._build(r2, "description+escalation", content, status, phase)
                        except Exception:
                            return self._build(r, "description", content, status, phase)
                    return self._build(r, "description", content, status, phase)
                return self._build(r, "description", "", "", "")

            # needs_scrape == true
            if self.args.description_only:
                # Take the thin description score; mark for a later scrape pass.
                return self._build(r, "description_thin", "", "", "")

        # Step 3 (NO branch): scrape ladder, then score the scraped text.
        return await self._scrape_then_score(name, domain, url)

    async def _scrape_then_score(self, name: str, domain: str, url: Optional[str]) -> Dict[str, Any]:
        content, status, phase = await self.scrape(url)
        if not content:
            # Scrape failed / no usable content (Bryan: score 1, Disqualified).
            return {
                "score": 1, "verdict": "Disqualified", "source": "scrape_failed",
                "reasoning": "no usable site content", "homepage_content": "",
                "homepage_status": status, "scrape_phase": phase, "error": None,
            }
        try:
            r = await self.call_llm(self.args.scrape_model, self.scrape_sys,
                                    scrape_user_prompt(name, domain, content), 400)
        except Exception as exc:
            return self._error(f"scrape-mode LLM error: {exc}", content, status, phase)

        # Escalation: borderline 5-7 with weak confidence -> smart model, same text.
        score = safe_int_score(r.get("score")) or 0
        conf = str(r.get("confidence", "")).lower()
        if self.escalate and ESCALATION_SCORE_LOWER <= score <= ESCALATION_SCORE_UPPER \
                and conf in ("low", "medium"):
            try:
                r = await self.call_llm(self.args.escalation_model, self.scrape_sys,
                                        scrape_user_prompt(name, domain, content), 400)
            except Exception:
                pass
        return self._build(r, "scrape", content, status, phase)


# ================================================================
# CSV / resume / progress
# ================================================================

def read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return headers, rows


def dedup_key(row: Dict[str, str], website_col: Optional[str], company_col: Optional[str]) -> str:
    if website_col:
        v = (row.get(website_col) or "").strip().lower()
        if v:
            return v
    if company_col:
        v = (row.get(company_col) or "").strip().lower()
        if v:
            return "name::" + v
    return ""


def load_done_keys(out_path: Path, website_col, company_col) -> set:
    """Resume: keys of rows already written with a non-empty ICP Score."""
    done = set()
    if not out_path.exists():
        return done
    try:
        with open(out_path, "r", encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if (r.get("ICP Score") or "").strip():
                    k = dedup_key(r, website_col, company_col)
                    if k:
                        done.add(k)
    except Exception:
        pass
    return done


def write_progress(path: Path, c: dict) -> None:
    data = {
        "scraped_done": c["scraped"],
        "scored_done": c["scored"],
        "qualified_count": c["qualified"],
        "httpx_hits": c["httpx_hits"],
        "zenrows_calls": c["zenrows_calls"],
        "errors": c["errors"],
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def estimate_and_confirm(pending: int, args) -> bool:
    """Print a cost estimate; prompt only on an interactive TTY for big runs."""
    desc_in, desc_out = 1500, 150
    scr_in, scr_out = 3500, 200
    desc_each = model_cost(args.description_model, desc_in, desc_out)
    scr_each = model_cost(args.scrape_model, scr_in, scr_out)
    assumed_scrape_rate = 0.40          # share of rows that fall through to scraping
    assumed_httpx_free = 0.65           # share of scraped rows fetched free by httpx
    scraped = pending * assumed_scrape_rate
    zr_paid = scraped * (1 - assumed_httpx_free)
    llm_est = pending * desc_each + scraped * scr_each
    zr_est = zr_paid * ((ZENROWS_COST_BASIC + ZENROWS_COST_JS) / 2)
    total = llm_est + zr_est

    print("\n=== Cost estimate (rough) ===")
    print(f"  Rows to process:        {pending:,}")
    print(f"  Description screen:     {args.description_model} (~{desc_in}/{desc_out} tok)")
    print(f"  Scrape scoring:         {args.scrape_model}")
    print(f"  Escalation:             {args.escalation_model} ({'on' if not args.no_escalation else 'off'})")
    print(f"  Assumptions:            ~{int(assumed_scrape_rate*100)}% of rows scrape, "
          f"~{int(assumed_httpx_free*100)}% of those free via httpx")
    print(f"  Est. LLM cost:          ${llm_est:,.2f}")
    print(f"  Est. ZenRows cost:      ${zr_est:,.2f}  (~{zr_paid:,.0f} paid scrapes)")
    print(f"  Est. TOTAL:             ${total:,.2f}")
    print("  (Estimate only; actual cost depends on how many descriptions are clear.)\n")

    if args.skip_confirm or pending <= 1000 or not sys.stdin.isatty():
        return True
    try:
        ans = input('Type "yes" to continue, anything else to abort: ').strip().lower()
    except EOFError:
        return True
    return ans == "yes"


# ================================================================
# Main async driver
# ================================================================

async def run(args, icp_def: str, in_headers: List[str], rows: List[Dict[str, str]],
              website_col, desc_col, company_col, out_path: Path,
              cache: ScrapeCache, progress_path: Path) -> dict:
    runner = Runner(args, icp_def)
    runner._cache = cache

    out_headers = list(in_headers) + [c for c in NEW_COLUMNS if c not in in_headers]
    done = load_done_keys(out_path, website_col, company_col)

    # Build pending list (skip already-scored rows for resume).
    pending = []
    skipped = 0
    for i, row in enumerate(rows):
        key = dedup_key(row, website_col, company_col)
        if key and key in done and not args.force_rescore:
            skipped += 1
            continue
        domain = (row.get(website_col) or "") if website_col else ""
        desc = (row.get(desc_col) or "") if desc_col else ""
        name = (row.get(company_col) or "") if company_col else ""
        if is_blank(domain) and is_blank(desc):
            continue  # nothing to work with
        pending.append((i, name, str(domain), str(desc)))
        if args.max_rows and len(pending) >= args.max_rows:
            break

    print(f"Total rows: {len(rows):,} | already scored (skipped): {skipped:,} | "
          f"to process: {len(pending):,}")
    if not pending:
        print("Nothing to do.")
        return runner.c

    if not estimate_and_confirm(len(pending), args):
        print("Aborted by user.")
        return runner.c

    # Open output for streaming append.
    out_is_new = not out_path.exists()
    out_f = open(out_path, "a", encoding="utf-8", newline="")
    writer = csv.DictWriter(out_f, fieldnames=out_headers, extrasaction="ignore")
    if out_is_new:
        writer.writeheader()
        out_f.flush()

    runner.aclient = httpx.AsyncClient(follow_redirects=True)
    bar = tqdm(total=len(pending), desc="scrape-and-score", unit="row",
               bar_format="{desc} [{bar:18}] {n:>6,}/{total:,} | {postfix}",
               ascii="░█", colour="green", smoothing=0.1)
    start = time.time()

    def postfix() -> str:
        c = runner.c
        scraped = c["scraped"] or 1
        httpx_pct = 100.0 * c["httpx_hits"] / scraped
        done_n = bar.n or 1
        rate = done_n / max(1e-9, time.time() - start)
        eta = int((len(pending) - bar.n) / rate / 60) if rate > 0 else 0
        return (f"Scraped: {c['scraped']:,} | Scored: {c['scored']:,} | "
                f"Qualified: {c['qualified']:,} | httpx%: {httpx_pct:.0f} free | "
                f"ZenRows: {c['zenrows_calls']:,} credits | ETA: {eta}min")

    write_lock = asyncio.Lock()
    queue: asyncio.Queue = asyncio.Queue()
    for item in pending:
        queue.put_nowait(item)

    async def worker():
        while True:
            try:
                idx, name, domain, desc = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                result = await runner.score_row(name, domain, desc)
            except Exception as exc:
                result = runner._error(f"unhandled: {type(exc).__name__}: {exc}")
            if result.get("error"):
                runner.c["errors"] += 1
            row = dict(rows[idx])
            row["homepage_content"] = result.get("homepage_content", "")
            row["homepage_status"] = result.get("homepage_status", "")
            row["scrape_phase"] = result.get("scrape_phase", "")
            row["ICP Score"] = result.get("score", "")
            row["ICP Verdict"] = result.get("verdict", "")
            row["Scoring Source"] = result.get("source", "")
            row["ICP Reasoning"] = result.get("reasoning", "")
            if str(result.get("verdict", "")) == "Qualified":
                runner.c["qualified"] += 1
            async with write_lock:
                writer.writerow(row)
                out_f.flush()
                runner.c["scored"] += 1
                bar.set_postfix_str(postfix(), refresh=False)
                bar.update(1)
                if runner.c["scored"] % 100 == 0:
                    write_progress(progress_path, runner.c)
            queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(args.concurrency)]
    try:
        await asyncio.gather(*workers)
    finally:
        bar.set_postfix_str(postfix(), refresh=True)
        bar.close()
        await runner.aclient.aclose()
        runner.llm_exec.shutdown(wait=True)
        out_f.close()
        write_progress(progress_path, runner.c)

    return runner.c


def write_qualified(scored_path: Path, qualified_path: Path, threshold: int) -> int:
    """Regenerate the qualified CSV from the scored output (dedup-safe)."""
    if not scored_path.exists():
        return 0
    headers, rows = read_csv(scored_path)
    n = 0
    with open(qualified_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            s = safe_int_score(r.get("ICP Score"))
            if s is not None and s >= threshold:
                writer.writerow(r)
                n += 1
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description="scrape-and-score: description-first ICP scoring with a free-httpx -> ZenRows scrape fallback.")
    parser.add_argument("csv_path", help="Path to input CSV")
    parser.add_argument("--icp", required=True, help="Path to ICP profile (.md/.txt) OR inline ICP text")
    parser.add_argument("--openrouter-key", required=True, help="OpenRouter API key (passed at runtime; never stored)")
    parser.add_argument("--zenrows-key", default="", help="ZenRows API key (passed at runtime; never stored)")
    parser.add_argument("--concurrency", type=int, default=HTTPX_CONCURRENCY,
                        help=f"Async row workers / httpx concurrency (default {HTTPX_CONCURRENCY})")
    parser.add_argument("--zenrows-concurrency", type=int, default=ZENROWS_CONCURRENCY,
                        help=f"Max concurrent ZenRows requests (default {ZENROWS_CONCURRENCY})")
    parser.add_argument("--score-workers", type=int, default=SCORE_WORKERS,
                        help=f"LLM thread-pool workers (default {SCORE_WORKERS})")
    parser.add_argument("--score-threshold", type=int, default=7,
                        help="Score >= threshold => Qualified (default 7)")
    parser.add_argument("--description-model", default=DEFAULT_DESCRIPTION_MODEL)
    parser.add_argument("--scrape-model", default=DEFAULT_SCRAPE_MODEL)
    parser.add_argument("--escalation-model", default=DEFAULT_ESCALATION_MODEL)
    parser.add_argument("--no-escalation", action="store_true", help="Disable borderline 5-7 escalation")
    parser.add_argument("--no-zenrows", action="store_true", help="Free httpx only; never call ZenRows")
    parser.add_argument("--scrape-only", action="store_true", help="Scrape+score every row; skip the description path")
    parser.add_argument("--description-only", action="store_true", help="Never scrape; score from description only")
    parser.add_argument("--force-rescore", action="store_true", help="Re-score rows already present in the output CSV")
    parser.add_argument("--skip-confirm", action="store_true", help="Skip the >1000-row cost confirmation prompt")
    parser.add_argument("--max-rows", type=int, help="Cap rows processed (testing)")
    parser.add_argument("--csv-out", help="Output path (default <input>_scored.csv)")
    args = parser.parse_args()

    if args.scrape_only and args.description_only:
        sys.stderr.write("ERROR: --scrape-only and --description-only are mutually exclusive.\n")
        return 2
    if not args.no_zenrows and not args.description_only and not args.zenrows_key:
        sys.stderr.write(
            "ERROR: --zenrows-key is required (the scrape fallback needs it).\n"
            "Pass --no-zenrows to scrape with free httpx only, or --description-only to skip scraping.\n"
        )
        return 2

    csv_in = Path(args.csv_path).expanduser().resolve()
    if not csv_in.exists():
        sys.stderr.write(f"ERROR: input CSV not found: {csv_in}\n")
        return 3

    folder = csv_in.parent
    base = csv_in.stem
    out_path = Path(args.csv_out).expanduser().resolve() if args.csv_out else folder / f"{base}_scored.csv"
    qualified_path = folder / f"{base}_qualified.csv"
    db_path = folder / "scrape_cache.db"
    progress_path = folder / "progress.json"

    icp_name, icp_def = load_icp(args.icp)
    in_headers, rows = read_csv(csv_in)
    if not rows:
        sys.stderr.write("ERROR: input CSV is empty.\n")
        return 3

    website_col = detect_column(in_headers, WEBSITE_KEYS)
    desc_col = detect_column(in_headers, DESCRIPTION_KEYS)
    company_col = detect_column(in_headers, COMPANY_KEYS)

    print(f"Input:   {csv_in}")
    print(f"Output:  {out_path}")
    print(f"ICP:     {icp_name} ({len(icp_def)} chars)")
    print(f"Columns: website={website_col!r} description={desc_col!r} company={company_col!r}")
    if not website_col and not desc_col:
        sys.stderr.write("ERROR: no website/domain column and no description column detected.\n")
        return 4
    mode = "scrape-only" if args.scrape_only else ("description-only" if args.description_only else "description-first hybrid")
    print(f"Mode:    {mode} | threshold={args.score_threshold} | "
          f"concurrency={args.concurrency} (httpx) / {args.zenrows_concurrency} (ZenRows) / "
          f"{args.score_workers} (LLM)")

    cache = ScrapeCache(str(db_path))
    print(f"Scrape cache: {len(cache._urls):,} domains in {db_path.name}")

    t0 = time.time()
    c = asyncio.run(run(args, icp_def, in_headers, rows, website_col, desc_col,
                        company_col, out_path, cache, progress_path))
    cache.close()
    elapsed = time.time() - t0

    n_qual = write_qualified(out_path, qualified_path, args.score_threshold)

    print("\n✅ Done")
    print(f"  Scored:        {c['scored']:,}")
    print(f"  Qualified:     {c['qualified']:,} (this run) | {n_qual:,} total in {qualified_path.name}")
    print(f"  Errors:        {c['errors']:,}")
    scraped = c["scraped"] or 1
    print(f"  Scraped:       {c['scraped']:,} (httpx free: {c['httpx_hits']:,} = "
          f"{100.0*c['httpx_hits']/scraped:.0f}%)")
    print(f"  ZenRows calls: {c['zenrows_calls']:,} (basic {c['zenrows_basic']:,} + js {c['zenrows_js']:,})")
    print(f"  Est. cost:     LLM ${c['llm_cost']:.4f} + ZenRows ${c['zenrows_cost']:.4f} "
          f"= ${c['llm_cost']+c['zenrows_cost']:.4f}")
    for model, calls in c["llm_calls"].items():
        print(f"                 {model}: {calls:,} calls")
    print(f"  Time:          {elapsed/60:.1f} min")
    print(f"  Output:        {out_path}")
    print(f"  Qualified:     {qualified_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
