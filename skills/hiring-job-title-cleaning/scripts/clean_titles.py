#!/usr/bin/env python3
"""
clean_titles.py — Clean raw scraped HR job titles into a natural-reading
canonical role title suitable as a cold-email merge variable:

    "I saw you're hiring for a {clean_title}"

STEP 1  Rule-based cleaning (free, no LLM) — handles the large majority.
STEP 2  LLM pass (openai/gpt-4o-mini via OpenRouter) for leftovers
        only (rows still containing digits / 3+ capital runs / pipes / >6 words).
STEP 3  Output CSV + dedupe report + flagged rows + samples + cost.

Usage:
    python clean_titles.py "INPUT.csv" [--col "Hiring Job Title"] [--no-llm]
        [--model X] [--concurrency N]
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ───────────────────────────── config ──────────────────────────────
OPENROUTER_KEY_FILE = os.path.expanduser("~/.olympolabs/openrouter.json")
# PRIMARY_MODEL: gpt-4o-mini is the proven default for concurrent batch title
# cleaning. google/gemini-2.5-flash-lite was REJECTED — it stalled twice under
# concurrent batch calls on OpenRouter (tail latency + burst throttling), so it
# is NOT used here. Do not re-promote it without re-testing under concurrency.
PRIMARY_MODEL = "openai/gpt-4o-mini"
FALLBACK_MODEL = "openai/gpt-4o-mini"
# gpt-4o-mini equivalent budget the user cares about (~$/row):
GPT4OMINI_PER_ROW = 0.0002

# ───────────────────────── rule vocab ──────────────────────────────

# Employment-type / logistics noise — matched as whole phrases (case-insensitive).
NOISE_PHRASES = [
    "full time year round", "full time", "part time", "year round",
    "temp to perm", "temporary to permanent", "now hiring", "co op", "co-op",
    "federal work study", "entry level", "2nd shift", "second shift",
    "3rd shift", "third shift", "1st shift", "first shift", "night shift",
    "day shift", "on site", "onsite", "on-site", "off site", "remote",
    "hybrid", "seasonal", "fractional", "contract position", "contract",
    "temp", "per diem", "w2", "1099", "ft days", "ft", "pt",
    "a plus", "bilingual a plus",
]

# Bilingual notes (whole-phrase, case-insensitive) stripped anywhere.
BILINGUAL_PHRASES = [
    "english spanish", "english/spanish", "spanish english", "bilingual",
]

# Location tokens: US state names + 2-letter postal codes + a few regions.
US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "ohio", "oklahoma", "oregon",
    "pennsylvania", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "wisconsin", "wyoming",
}
STATE_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy",
}
# Multi-word regions / metros that appear as trailing geography.
REGION_PHRASES = [
    "new england", "south central", "bay area", "americas", "tri state",
    "tri-state", "mid atlantic", "midwest", "northeast", "southeast",
    "southwest", "northwest", "pacific northwest", "greater boston",
]
# Standalone city tokens seen trailing in this dataset (kept conservative —
# only well-known unambiguous cities, never role words).
CITY_TOKENS = {
    "boston", "charlotte", "houston", "dallas", "tampa", "vancouver",
    "coolidge", "pelham", "atlanta", "chicago", "denver", "phoenix",
    "seattle", "austin", "nashville", "orlando", "miami", "philadelphia",
}

# Roman-numeral level markers (trailing).
ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii"}

# Leadership "head" nouns that read best with "of" (head-first → "Head of X").
SENIOR_HEADS = {
    "director", "president", "officer", "head", "chief", "superintendent",
    "chair", "chairperson",
}
# Mid-level "head" nouns that read best as a prefix ("Human Resources Manager").
MID_HEADS = {
    "manager", "generalist", "coordinator", "specialist", "analyst",
    "administrator", "representative", "technician", "assistant", "advisor",
    "consultant", "partner", "recruiter", "liaison", "leader", "professional",
    "associate", "clerk",
}
# Words that are pure level/seniority modifiers (kept, but ignored when
# deciding whether a remainder is "just a generic head").
LEVEL_WORDS = {
    "senior", "junior", "lead", "principal", "staff", "entry", "associate",
    "regional", "district", "global", "corporate", "dual", "interim", "us",
    "executive", "chief", "vice", "deputy", "assistant", "national",
}

# Title-case small words that stay lowercase unless first.
SMALL_WORDS = {"of", "and", "the", "for", "to", "in", "on", "at", "a", "an",
               "or", "with", "by"}

# Acronyms / brand-y tokens that must keep their casing in Title Case.
KEEP_UPPER = {"HRIS", "HRBP", "CHRO", "CPO", "CDO", "HR", "G&A", "GTM",
              "HRWD", "PEO", "FMLA", "US", "AY", "IT", "EHS",
              "VP", "SVP", "EVP", "PMC", "PWM"}


# ───────────────────────── rule cleaning ───────────────────────────

def _strip_parens(s: str) -> str:
    """Remove (…) groups, including unclosed trailing '(' fragments."""
    s = re.sub(r"\([^)]*\)", " ", s)        # closed parens
    s = re.sub(r"\([^)]*$", " ", s)         # unclosed trailing paren
    s = re.sub(r"\[[^\]]*\]", " ", s)       # square brackets too
    return s


def _strip_codes(s: str) -> str:
    """Remove job IDs / alphanumeric codes."""
    # alphanumeric mixes like AFODC5826005
    s = re.sub(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{4,}\b", " ", s)
    # short code + number: "PR 1392"
    s = re.sub(r"\b[A-Z]{2,4}\s+\d{2,}\b", " ", s)
    # bare standalone number runs
    s = re.sub(r"\b\d{2,}\b", " ", s)
    return s


def _normalize_seps(s: str) -> str:
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _remove_phrases(s: str, phrases) -> str:
    for p in sorted(phrases, key=len, reverse=True):
        s = re.sub(r"(?<![A-Za-z])" + re.escape(p) + r"(?![A-Za-z])",
                   " ", s, flags=re.IGNORECASE)
    return s


def _strip_leading(s: str) -> str:
    """Strip leading boilerplate prefixes."""
    leaders = [r"now hiring", r"title", r"job", r"hiring"]
    changed = True
    while changed:
        changed = False
        for L in leaders:
            new = re.sub(r"^\s*" + L + r"\b[\s:–—-]*", "", s, flags=re.IGNORECASE)
            if new != s:
                s, changed = new, True
    return s


def _strip_trailing_geo(s: str) -> str:
    """Strip trailing locations: regions, cities, states, abbreviations."""
    s = _remove_phrases(s, REGION_PHRASES)
    s = _normalize_seps(s)
    # iteratively peel trailing geo tokens
    changed = True
    while changed:
        changed = False
        toks = s.split()
        if not toks:
            break
        last = toks[-1].lower().strip(".,")
        # "City ST" pair
        if len(toks) >= 2:
            last2 = toks[-2].lower().strip(".,")
            if last in STATE_ABBR and (last2 in CITY_TOKENS or last2 in US_STATES):
                s = " ".join(toks[:-2]); changed = True; continue
        if last in US_STATES or last in CITY_TOKENS:
            s = " ".join(toks[:-1]); changed = True; continue
        # lone trailing 2-letter postal code (e.g. "... CT")
        if last in STATE_ABBR and toks[-1].isupper():
            s = " ".join(toks[:-1]); changed = True; continue
    return s


def _strip_trailing_levels(s: str) -> str:
    """Strip trailing roman numerals and stray junk fragments."""
    changed = True
    while changed:
        changed = False
        toks = s.split()
        if not toks:
            break
        last = toks[-1].lower().strip(".,")
        if last in ROMAN:
            s = " ".join(toks[:-1]); changed = True; continue
        # stray 1-2 char trailing fragments that aren't a known word
        if len(last) <= 2 and last not in {"hr"} and not last.isdigit():
            # only drop if it looks like junk (not 'of'/'&')
            if last not in SMALL_WORDS and last != "&":
                s = " ".join(toks[:-1]); changed = True; continue
    return s


def _fix_sr(s: str) -> str:
    # \bSr\b then optionally swallow a trailing '.' (the dot is NOT inside the
    # \b, so "\bSr\.?\b" would leave the period behind -> "Senior. Director").
    s = re.sub(r"\bSr\b\.?", "Senior", s, flags=re.IGNORECASE)
    s = re.sub(r"\bJr\b\.?", "Junior", s, flags=re.IGNORECASE)
    return s


def _expand_vp(s: str) -> str:
    # Only expand the clean 2-letter VP (helps dedup). SVP/EVP/AVP are left
    # intact so their 3+ capital run routes them to the LLM, which expands
    # them in context (matches the user's examples).
    s = re.sub(r"\bVP\b", "Vice President", s)
    return s


def _resolve_trailing_hr(s: str) -> str:
    """
    Handle the 'Human Resources' department echo & head-first ordering.
      'Recruiter Human Resources'   -> 'Recruiter'        (complete role)
      'Manager Human Resources'     -> 'Human Resources Manager'
      'Director Human Resources'    -> 'Director of Human Resources'
      'Generalist Human Resources'  -> 'Human Resources Generalist'
    """
    m = re.search(r"^(.*?)\s+human resources?$", s, flags=re.IGNORECASE)
    if not m:
        return s
    remainder = m.group(1).strip()
    if not remainder:
        return s  # title literally is "Human Resources"
    # If HR is joined by a connector it's a genuine object, already well-formed
    # ("Director of Human Resources", "VP for Human Resources") — leave it.
    if remainder.lower().rstrip().endswith(("of", "and", "&", ",", "for")):
        return s
    toks = remainder.split()
    low = [t.lower() for t in toks]

    # Senior leadership head present -> "<remainder> of Human Resources",
    # preserving word order so "Vice President" is never split.
    if any(t in SENIOR_HEADS for t in low):
        return remainder + " of Human Resources"

    # Bare mid head (only level modifiers around it) -> prefix form,
    # EXCEPT roles that stand alone ("Recruiter Human Resources" -> "Recruiter").
    non_level = [t for t in toks if t.lower() not in LEVEL_WORDS]
    if (len(non_level) == 1 and non_level[0].lower() in MID_HEADS
            and non_level[0].lower() not in {"recruiter", "sourcer"}):
        levels = [t for t in toks if t.lower() in LEVEL_WORDS]
        return " ".join(levels + ["Human", "Resources"] + non_level)

    # Otherwise the remainder is already a complete role -> drop the dept echo.
    return remainder


def _insert_of(s: str) -> str:
    """
    Head-first leadership titles read better with 'of':
      'Director Talent Acquisition' -> 'Director of Talent Acquisition'
      'VP Total Rewards'            -> 'Vice President of Total Rewards'  (post-expand)
    Only fires when a senior head is immediately followed by an object and
    there is no existing 'of'/'&'/'and'/'-' joining them.
    """
    toks = s.split()
    low = [t.lower() for t in toks]
    if "of" in low or "for" in low:      # already has a connector
        return s
    # use the RIGHTMOST senior head so leading modifiers like 'Chief … Officer'
    # aren't mistaken for the head ("Chief Human Resources Officer" stays put).
    head_idx = None
    for i, t in enumerate(low):
        if t in SENIOR_HEADS:
            head_idx = i
    if head_idx is None or head_idx + 1 >= len(toks):
        return s
    obj = toks[head_idx + 1:]
    nxt = obj[0].lower()
    if nxt in {"&", "and", "-"}:
        return s
    # skip when the object itself ends in a role head ("Director People Partner")
    if obj[-1].lower() in MID_HEADS or obj[-1].lower() in SENIOR_HEADS:
        return s
    return " ".join(toks[: head_idx + 1] + ["of"] + obj)


def _titlecase(s: str) -> str:
    out = []
    toks = s.split()
    for i, w in enumerate(toks):
        up = w.upper().strip(".,")
        if up in KEEP_UPPER:
            out.append(up)
            continue
        if up in {"I", "II", "III", "IV", "V", "VI"}:   # roman numerals
            out.append(up)
            continue
        lw = w.lower()
        if lw in SMALL_WORDS and i != 0 and i != len(toks) - 1:
            out.append(lw)
        elif "&" in w:
            out.append(w)
        elif "-" in w and len(w) > 1:
            out.append("-".join(p.capitalize() for p in w.split("-")))
        else:
            out.append(w[:1].upper() + w[1:].lower() if w else w)
    return " ".join(out)


def _dedupe_slash(s: str) -> str:
    """'Manager Director' style duplicate heads -> keep the primary (first)."""
    s = re.sub(r"\bManager\s+Director\b", "Manager", s, flags=re.IGNORECASE)
    s = re.sub(r"\bManager\s*/\s*Director\b", "Manager", s, flags=re.IGNORECASE)
    s = re.sub(r"\bCoordinator\s+I\s+II\b", "Coordinator", s, flags=re.IGNORECASE)
    # "Director Senior Director" -> "Senior Director" (duplicate leadership head)
    s = re.sub(r"\bDirector\s+(Senior|Junior|Lead)\s+Director\b", r"\1 Director",
               s, flags=re.IGNORECASE)
    return s


def rule_clean(raw: str) -> str:
    s = raw.strip()
    # take first pipe segment (company / location / fluff live after pipes)
    if "|" in s:
        s = s.split("|")[0]
    # company introduced by "@" or " at " runs to end of string
    s = s.split("@")[0]
    s = re.sub(r"\s+\bat\b\s+.+$", "", s, flags=re.IGNORECASE)
    s = _strip_parens(s)
    s = _remove_phrases(s, BILINGUAL_PHRASES)
    s = _normalize_seps(s)
    s = _strip_leading(s)
    s = _fix_sr(s)
    s = _expand_vp(s)
    s = _strip_codes(s)
    s = _remove_phrases(s, NOISE_PHRASES)
    s = _normalize_seps(s)
    # drop trailing dangling separators / "a plus!" leftovers
    s = re.sub(r"[•·,;:!]+", " ", s)
    s = _normalize_seps(s)
    s = _strip_trailing_geo(s)
    s = _dedupe_slash(s)
    s = _resolve_trailing_hr(s)
    s = _strip_trailing_levels(s)
    s = _normalize_seps(s)
    s = _insert_of(s)
    # trim trailing/leading dashes & stray punctuation
    s = s.strip(" -–—.,&/")
    s = _normalize_seps(s)
    # move a trailing lone "Senior"/level to the front (e.g. "... Senior")
    toks = s.split()
    if len(toks) > 1 and toks[-1].lower() in {"senior", "junior", "lead"}:
        s = toks[-1] + " " + " ".join(toks[:-1])
    # NOTE: title-casing is applied later (in main) so needs_llm() can still
    # see ALL-CAPS company tags (LVHN, ROCK, …) that signal a leftover.
    return s.strip()


# ───────────────────── dirty / leftover detection ──────────────────

def needs_llm(s: str) -> bool:
    if not s:
        return False
    if re.search(r"\d", s):
        return True
    if re.search(r"[A-Z]{3,}", s):            # 3+ capital run (acronym/company)
        return True
    if "|" in s:
        return True
    if len(s.split()) > 6:
        return True
    return False


def looks_dirty(s: str) -> bool:
    """Heuristic for flagging rows that still look unclean for manual review."""
    if not s:
        return True
    if re.search(r"\d", s):
        return True
    if "|" in s or "(" in s or ")" in s:
        return True
    # stray uppercase company-acronym leftover
    if re.search(r"\b[A-Z]{3,}\b", s) and not re.search(
            r"\b(HRIS|HRBP|CHRO|CPO|CDO|HRWD|PEO|FMLA)\b", s):
        return True
    if len(s.split()) > 7:
        return True
    return False


# ─────────────────── short_title rule shortening ───────────────────

# Company / location remnants the first pass may leave behind.
SHORT_JUNK = [
    "grand bank", "kettering foundation", "playcore", "villa hermosa",
    "corporate restaurants", "houston office", "components division",
    "custom glass products", "adhesive solutions", "private security industry",
    "doubletree hotel charlottesville", "doubletree", "monroe community college",
    "woodlands nursing and rehabilitation center", "dick hannah dealerships",
    "holt truck centers", "pacs services", "pacs", "manufacturing", "home care",
    "corporate office", "corporate", "district", "springer", "stuart",
    "components", "adhc", "sdg",
]
# Heads that read more naturally reordered ("Director of X" -> "X Director").
SHORT_REORDER_HEADS = {
    "director", "manager", "coordinator", "specialist", "generalist",
    "administrator", "analyst", "recruiter", "partner", "representative",
    "technician", "liaison",
}
# Real head NOUNS (NOT pure modifiers like chief/vice/head) — used to decide
# whether a left clause is a complete role before dropping a secondary function.
_HEAD_TOKENS = {
    "director", "manager", "coordinator", "specialist", "generalist",
    "administrator", "analyst", "representative", "technician", "assistant",
    "advisor", "consultant", "partner", "recruiter", "liaison", "officer",
    "president", "vp", "svp", "evp", "chro", "cpo",
}


def _has_head(s: str) -> bool:
    low = s.lower().replace("&", " ").split()
    return any(t.strip(".,") in _HEAD_TOKENS for t in low)


def _drop_secondary(s: str) -> str:
    """Keep only the primary function: cut at first ' and '/' & ' when the left
    clause already carries a real head ('Director of HR & Recruiting' -> left).
    Commas are handled as 'of' reordering, not dropping, so 'Director, Total
    Rewards' keeps its function."""
    parts = re.split(r"(?i)\s+(?:and|&)\s+", s, maxsplit=1)
    if len(parts) > 1 and _has_head(parts[0]):
        return parts[0].strip()
    return s


def short_rules(clean: str):
    """Rule-based shortening of a clean_title. Returns (short_title, flag)."""
    s = (clean or "").strip()
    if not s:
        return "", True
    # safety repairs for first-pass breakage
    s = re.sub(r"(?i)\bsenior\.\s*", "Senior ", s)
    # 'Head, Object' -> 'Head of Object' (comma is a department separator)
    s = re.sub(r"(?i)^(.*?\b(?:director|manager|coordinator|specialist|"
               r"generalist|officer|president|vp|svp|administrator|analyst|"
               r"partner)),\s+", r"\1 of ", s)
    # 'Vice Global/Regional President' -> 'Global VP'
    m = re.match(r"(?i)^vice\s+(global|regional|national)\s+president\b(.*)$", s)
    if m:
        s = f"{m.group(1).title()} VP{m.group(2)}"
    # drop company / location remnants
    for j in sorted(SHORT_JUNK, key=len, reverse=True):
        s = re.sub(r"(?i)(?:\bof\s+)?\b" + re.escape(j) + r"\b", " ", s)
    s = _normalize_seps(s).strip(" -–—.,&/")
    # drop secondary function
    s = _drop_secondary(s)
    # abbreviate (CHRO before HR so it isn't half-abbreviated)
    s = re.sub(r"(?i)\bChief Human Resources? Officer\b", "CHRO", s)
    s = re.sub(r"(?i)\bSenior Vice President\b", "SVP", s)
    s = re.sub(r"(?i)\bVice President\b", "VP", s)
    s = re.sub(r"(?i)\bHuman Resources?\b", "HR", s)
    # 'VP Global of HR' / 'VP Global HR' -> 'Global VP'
    s = re.sub(r"(?i)\bVP\s+(global|regional|national)\s+(?:of\s+)?HR\b",
               r"\1 VP", s)
    s = _normalize_seps(s)
    # reorder '<head> of <X>' -> '<X> <head>' for non-VP heads (shorter speech)
    m = re.match(r"(?i)^(senior\s+)?(" + "|".join(SHORT_REORDER_HEADS) +
                 r")\s+of\s+(.+)$", s)
    if m and " of " not in m.group(3).lower():
        pre = (m.group(1) or "").strip()
        s = " ".join(x for x in [pre, m.group(3), m.group(2)] if x)
    s = _normalize_seps(s).strip(" -–—.,&/")
    s = _titlecase(s)
    # flag broken / role-less leftovers for review
    flag = False
    bare = s.lower().replace(".", "").strip()
    if not s or bare in {"hr", "human resources", "human resource", "people"}:
        flag = True
    if re.search(r"(?i)\bfor\b\s*$", s) or re.search(r"(?i)\bof for\b", s):
        flag = True
    return s, flag


def _normalize_short(s: str) -> str:
    """Final consistency pass so equivalent short titles dedupe together."""
    if not s:
        return s
    s = re.sub(r"(?i)\bSenior VP\b", "SVP", s)
    s = re.sub(r"(?i)\bComp\b", "Compensation", s)
    return _normalize_seps(s)


def short_needs_llm(s: str) -> bool:
    if not s:
        return False
    if len(s.split()) > 4:
        return True
    if re.search(r"\d|\||\(|\)", s):
        return True
    if re.search(r"\b[A-Z]{4,}\b", s):   # long all-caps = stray company tag
        return True
    return False


def short_looks_dirty(s: str) -> bool:
    if not s:
        return True
    bare = s.lower().replace(".", "").strip()
    if bare in {"hr", "human resources", "human resource", "people"}:
        return True
    if len(s.split()) > 4:
        return True
    if re.search(r"\d|\||\(|\)", s):
        return True
    if re.search(r"(?i)\bfor\b\s*$|\bof for\b", s):
        return True
    return False


# ───────────────────────────── LLM pass ────────────────────────────

CLEAN_SYSTEM = (
    "Clean this scraped job title into the canonical role title only - remove "
    "company names, locations, codes, shifts, employment type. It must read "
    "naturally in: 'I saw you're hiring for a {title}'. "
    'Return {"clean_title": "..."}.\n'
    "Examples:\n"
    "'Human Resources Business Partner II | KanPak | Penn Yan NY' -> "
    "'Human Resources Business Partner'\n"
    "'Sr. Director Talent Acquisition | Unique Ground Floor Oppty | Well Funded "
    "& Rapidly Scaling Biotech | NJ' -> 'Senior Director of Talent Acquisition'\n"
    "'HUMAN RESOURCES SPECIALIST WOODLANDS NURSING AND REHABILITATION CENTER' -> "
    "'Human Resources Specialist'\n"
    "If you cannot produce a clean canonical title, return "
    '{"clean_title": ""}. Do not guess.'
)

SHORT_SYSTEM = (
    "Shorten this job title to how a person would casually say it in "
    "conversation, 2-4 words. It must read naturally in: 'I saw you're hiring "
    "for a {title}'. Use common abbreviations (HR, VP, CHRO, HRBP is OK for "
    "Business Partner roles). Keep seniority. Drop secondary functions, "
    "departments, and qualifiers. "
    'Return {"short_title": "..."}.\n'
    "Examples:\n"
    "'Senior Director of Integrated Talent and People Strategy' -> "
    "'Senior Talent Director'\n"
    "'Human Resources Business Partner' -> 'HR Business Partner'\n"
    "'Director of People Operations and Talent Strategy' -> 'People Ops Director'\n"
    "'Coordinator of Human Resources Employee Benefits' -> 'HR Benefits Coordinator'\n"
    "If you cannot shorten it sensibly, return "
    '{"short_title": ""}. Do not guess.'
)


def llm_call(text: str, api_key: str, model: str, system: str, key: str,
             retries: int = 2):
    """Generic OpenRouter JSON call. Returns (value or None, usage_dict)."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "usage": {"include": True},
    }
    data = None
    for attempt in range(retries):
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "clean_titles",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (429, 500, 502, 503) and attempt < retries - 1:
                continue
            sys.stderr.write(f"[LLM HTTP {code}] {e.read().decode('utf-8')[:160]}\n")
            return None, {}
        except Exception as e:
            if attempt < retries - 1:
                continue
            sys.stderr.write(f"[LLM error] {e}\n")
            return None, {}

    usage = (data or {}).get("usage", {}) or {}
    try:
        content = data["choices"][0]["message"]["content"]
        val = (json.loads(content).get(key) or "").strip()
        return (val if val else None), usage
    except Exception:
        return None, usage


def run_llm_batch(items, api_key, model, system, key, concurrency):
    """Concurrently clean a list of distinct strings. Returns (results, usages)."""
    results, usages = {}, []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(llm_call, it, api_key, model, system, key): it
                for it in items}
        for fut in as_completed(futs):
            it = futs[fut]
            val, usage = fut.result()
            results[it] = val
            usages.append(usage)
    return results, usages


def _sum_usage(usages):
    p = sum((u.get("prompt_tokens") or 0) for u in usages)
    c = sum((u.get("completion_tokens") or 0) for u in usages)
    cost = sum((u.get("cost") or 0.0) for u in usages)
    return p, c, cost


# ───────────────────────────── main ────────────────────────────────

def _cost_guard(label, usages, model_used):
    p, c, cost = _sum_usage(usages)
    n = len(usages) or 1
    per_row = cost / n
    if per_row > GPT4OMINI_PER_ROW and model_used == PRIMARY_MODEL:
        sys.stderr.write(
            f"\n[!] {label}: {model_used} billed ${per_row:.6f}/row > gpt-4o-mini "
            f"equivalent ${GPT4OMINI_PER_ROW}/row (likely hidden reasoning "
            f"tokens). Consider --model {FALLBACK_MODEL}.\n\n")
    return p, c, cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--col", default="Hiring Job Title")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--model", default=PRIMARY_MODEL)
    ap.add_argument("--concurrency", type=int, default=15)
    args = ap.parse_args()

    import csv
    csv.field_size_limit(10**9)

    with open(args.input, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    # Cap concurrency: small batches throttle when burst > 30, and there is no
    # point spawning more workers than rows. Default 15, hard ceiling min(rows, 30).
    ceiling = min(len(rows), 30) or 1
    if args.concurrency > ceiling:
        sys.stderr.write(f"[info] concurrency {args.concurrency} -> capped at "
                         f"{ceiling} (min(rows, 30))\n")
        args.concurrency = ceiling

    col = args.col
    if col not in (fieldnames or []):
        col = (fieldnames or ["title"])[0]
        sys.stderr.write(f"[warn] column not found, using '{col}'\n")

    api_key = None
    if not args.no_llm:
        try:
            api_key = json.load(open(OPENROUTER_KEY_FILE))["api_key"]
        except Exception as e:
            sys.stderr.write(f"[warn] no OpenRouter key ({e}); skipping LLM\n")
    model_used = args.model

    # ════════════ PASS A — clean_title ════════════
    distinct = {}
    for r in rows:
        raw = (r.get(col) or "").strip()
        if raw not in distinct:
            distinct[raw] = rule_clean(raw)

    leftovers = [raw for raw, cl in distinct.items() if needs_llm(cl)]
    clean_llm, clean_empty = {}, set()
    cl_p = cl_c = cl_cost = 0.0
    if api_key and leftovers:
        sys.stderr.write(f"[info] PASS A: {len(leftovers)} leftovers -> LLM "
                         f"(conc={args.concurrency})\n")
        res, usages = run_llm_batch(leftovers, api_key, model_used,
                                    CLEAN_SYSTEM, "clean_title", args.concurrency)
        for raw, val in res.items():
            if val:
                clean_llm[raw] = val
            else:
                clean_empty.add(raw)
        cl_p, cl_c, cl_cost = _cost_guard("clean_title", usages, model_used)

    clean_final = {}
    for raw, cl in distinct.items():
        if raw in clean_llm:
            clean_final[raw] = clean_llm[raw]
        elif raw in clean_empty:
            clean_final[raw] = ""
        else:
            clean_final[raw] = _titlecase(cl)

    # ════════════ PASS B — short_title (built on clean_title) ════════════
    short_distinct = {}          # clean_title -> (short, flag)
    for ct in set(clean_final.values()):
        short_distinct[ct] = short_rules(ct)

    short_leftovers = [ct for ct, (sh, fl) in short_distinct.items()
                       if ct and (short_needs_llm(sh) or fl)]
    short_llm, short_empty = {}, set()
    sh_p = sh_c = sh_cost = 0.0
    if api_key and short_leftovers:
        sys.stderr.write(f"[info] PASS B: {len(short_leftovers)} leftovers -> LLM "
                         f"(conc={args.concurrency})\n")
        res, usages = run_llm_batch(short_leftovers, api_key, model_used,
                                    SHORT_SYSTEM, "short_title", args.concurrency)
        for ct, val in res.items():
            if val:
                short_llm[ct] = val
            else:
                short_empty.add(ct)
        sh_p, sh_c, sh_cost = _cost_guard("short_title", usages, model_used)

    short_final = {}
    for ct, (sh, fl) in short_distinct.items():
        if ct in short_llm:
            short_final[ct] = _normalize_short(_titlecase(short_llm[ct]))
        elif ct in short_empty:
            short_final[ct] = ""
        else:
            short_final[ct] = _normalize_short(sh)

    # ════════════ write output ════════════
    out_path = re.sub(r"\.csv$", "", args.input) + "_cleaned.csv"
    out_fields = list(fieldnames) + ["clean_title", "short_title", "needs_review"]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        for r in rows:
            raw = (r.get(col) or "").strip()
            ct = clean_final.get(raw, "")
            sh = short_final.get(ct, "")
            review = "YES" if (not ct or looks_dirty(ct)
                               or not sh or short_looks_dirty(sh)) else ""
            r["clean_title"] = ct
            r["short_title"] = sh
            r["needs_review"] = review
            w.writerow(r)

    # ════════════ reports ════════════
    from collections import Counter

    print("=" * 70)
    print("SAMPLES  (clean_title -> short_title)")
    print("=" * 70)
    shown, seen = 0, set()
    for r in rows:
        raw = (r.get(col) or "").strip()
        ct = clean_final.get(raw, "")
        if ct in seen:
            continue
        seen.add(ct)
        sh = short_final.get(ct, "")
        tag = "  [LLM]" if ct in short_llm or ct in short_empty else ""
        print(f"{ct[:46]:46s} -> {sh}{tag}")
        shown += 1
        if shown >= 30:
            break

    print("\n" + "=" * 70)
    print("STEP 2  LLM COST  (model: %s, concurrency: %d)"
          % (model_used, args.concurrency))
    print("-" * 70)
    print(f"  clean_title : {len(leftovers):3d} rows | "
          f"tok {int(cl_p)}+{int(cl_c)} | ${cl_cost:.6f}")
    print(f"  short_title : {len(short_leftovers):3d} rows | "
          f"tok {int(sh_p)}+{int(sh_c)} | ${sh_cost:.6f}")
    print(f"  TOTAL BILLED: ${cl_cost + sh_cost:.6f}")
    print("=" * 70)

    scounts = Counter(short_final.get(clean_final.get((r.get(col) or '').strip(),
                      ""), "") for r in rows)
    scounts.pop("", None)
    print(f"\nDISTINCT short_title — {len(rows)} rows -> {len(scounts)} titles")
    print("-" * 70)
    for title, c in scounts.most_common():
        print(f"{c:4d}  {title}")

    review_distinct = sorted({(r["clean_title"], r["short_title"])
                              for r in rows if r["needs_review"] == "YES"})
    print("\n" + "=" * 70)
    print(f"FLAGGED FOR MANUAL REVIEW: {len(review_distinct)} distinct")
    print("=" * 70)
    for ct, sh in review_distinct:
        print(f"  clean: {ct[:60]!r:62s} short: {sh!r}")

    print(f"\nOutput written: {out_path}")


if __name__ == "__main__":
    main()
