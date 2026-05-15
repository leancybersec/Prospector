#!/usr/bin/env python3
"""
===============================================================================
  PROSPECTOR TEMPLATE — Generic B2B lead-gen script for MSPs
===============================================================================

HOW TO USE THIS FILE
--------------------
1. Upload this file to Claude (claude.ai).
2. Tell Claude what you want, for example:
       "Customize this script for me. I want to find:
        - Roofing and HVAC companies
        - In Phoenix, AZ and surrounding towns within 30 miles
        - With 10 to 75 employees
        - 15 prospects per day"
3. Claude will edit the CONFIG block below with your values and hand the file
   back. Save it, set up the .env file, install dependencies, and run.

WHAT THIS SCRIPT DOES
---------------------
- Finds qualifying companies using Claude Haiku + the Anthropic web_search tool.
  (Haiku is the default for cost/speed; switch MODEL to a Sonnet ID below if
  you want higher-quality inference at higher token cost.)
- Dedupes against allprospects.csv by normalized website domain.
- Appends new hits to allprospects.csv and writes newprospects_MMDDYYYY.csv.
- Writes a per-day log file: logs/prospector_MMDDYYYY.log.

REQUIREMENTS
------------
- Python 3.10+
- pip install anthropic python-dotenv
- A .env file next to this script containing: ANTHROPIC_API_KEY=sk-ant-...
- Ubuntu/Linux/macOS (Windows works too, paths are handled correctly).

SCHEDULING (Ubuntu cron, daily at 6 AM)
---------------------------------------
    0 6 * * * cd /home/USER/prospector && /home/USER/prospector/.venv/bin/python prospector.py >> logs/cron.log 2>&1

===============================================================================
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from anthropic import Anthropic, APIError, APIStatusError, RateLimitError
from dotenv import load_dotenv


# =============================================================================
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>  CONFIG  <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
# =============================================================================
# CLAUDE: When a user asks you to customize this script, edit ONLY the values
# inside this CONFIG block. Do not change any code below the CONFIG block
# unless the user explicitly asks for a behavior change.
#
# Required values the user must provide (ask them if they didn't):
#   1. PRIMARY_CITY        - e.g. "Denver, CO" or "Phoenix, AZ"
#   2. SURROUNDING_TOWNS   - list of nearby towns/cities to include in search
#   3. INDUSTRIES          - list of industries with target counts
#   4. MIN_EMPLOYEES       - minimum company size (e.g. 10)
#   5. MAX_EMPLOYEES       - maximum company size (e.g. 200)
#
# If the user doesn't specify SURROUNDING_TOWNS, use your knowledge of the
# region to populate reasonable ones within ~30 miles of the primary city.
# If the user doesn't specify per-industry counts, split the total evenly.
# If the user doesn't specify a total, default to 20.
# =============================================================================

# --- Primary location ---
PRIMARY_CITY = "Denver, CO"

# Surrounding towns / suburbs to include in the search. Claude: populate these
# with real places within ~30 miles of PRIMARY_CITY if the user doesn't list them.
SURROUNDING_TOWNS = [
    "Aurora", "Lakewood", "Thornton", "Arvada", "Westminster",
    "Centennial", "Englewood", "Wheat Ridge", "Littleton", "Highlands Ranch",
    "Commerce City", "Northglenn", "Broomfield", "Golden", "Parker",
    "Lone Tree", "Greenwood Village", "Boulder", "Louisville", "Lafayette",
    "Brighton",
]

# --- Industries to target ---
# Each entry: name, count (how many to find), guidance (what counts as this industry).
# Claude: if the user gives industry names without guidance, write a one-sentence
# guidance string yourself describing what kinds of companies qualify.
INDUSTRIES = [
    {
        "name": "construction",
        "count": 20,
        "guidance": (
            "General contractors, commercial and residential builders, "
            "civil/heavy construction, and specialty trade contractors "
            "(electrical, plumbing, HVAC, roofing, concrete, etc.)."
        ),
    },
    {
        "name": "finance",
        "count": 20,
        "guidance": (
            "Banks, credit unions, registered investment advisors (RIAs), "
            "wealth management firms, accounting/CPA firms, insurance "
            "agencies, lenders, and fintech companies."
        ),
    },
]

# --- Company size range ---
MIN_EMPLOYEES = 10
MAX_EMPLOYEES = 200

# =============================================================================
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>  END OF CONFIG  <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
# =============================================================================
# Nothing below this line typically needs to be edited.
# =============================================================================


# ---------------------------------------------------------------------------
# Paths & API constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ALL_PROSPECTS_CSV = SCRIPT_DIR / "allprospects.csv"
LOG_DIR = SCRIPT_DIR / "logs"

# Model selection.
# - "claude-haiku-4-5" — default. Fast, cheap, good enough for extraction tasks
#   like this. Has its own rate-limit bucket separate from Sonnet's.
# - "claude-sonnet-4-5" — higher-quality reasoning, better edge-case judgment
#   (geographic, industry, headcount), ~3x the input cost. Swap in if Haiku
#   results aren't precise enough.
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 2000
MAX_API_ATTEMPTS = 4
RETRY_BASE_DELAY = 5          # seconds, exponential backoff
WEB_SEARCH_MAX_USES = 5       # per industry call
INTER_INDUSTRY_SLEEP = 30     # seconds between industry calls (rate-limit buffer)

CSV_COLUMNS = ["company_name", "website"]
TARGET_TOTAL = sum(ind["count"] for ind in INDUSTRIES)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"prospector_{datetime.now():%m%d%Y}.log"

    logger = logging.getLogger("prospector")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


# ---------------------------------------------------------------------------
# Domain normalization & dedup
# ---------------------------------------------------------------------------

def normalize_domain(website: str) -> str:
    """
    Reduce a website URL to a canonical domain for dedup.
    'https://www.ABC-Builders.com/about/' -> 'abc-builders.com'
    """
    if not website:
        return ""

    w = website.strip().lower()
    if not w.startswith(("http://", "https://")):
        w = "https://" + w

    try:
        host = urlparse(w).netloc
    except ValueError:
        return ""

    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    return host


def load_existing_domains(path: Path) -> set[str]:
    if not path.exists():
        return set()

    domains: set[str] = set()
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = normalize_domain(row.get("website", ""))
            if d:
                domains.add(d)
    return domains


# ---------------------------------------------------------------------------
# Anthropic API call (Claude + web_search tool)
# ---------------------------------------------------------------------------

def build_prompt(industry: dict, excluded_domains: set[str]) -> str:
    """
    Build the user prompt for one industry. The model uses the web_search tool
    to find real companies and returns a JSON array.
    """
    # Build the list of locations to search across
    primary_city_name = PRIMARY_CITY.split(",")[0].strip()
    state_part = PRIMARY_CITY.split(",")[1].strip() if "," in PRIMARY_CITY else ""
    all_locations = [primary_city_name] + [
        t for t in SURROUNDING_TOWNS if t.lower() != primary_city_name.lower()
    ]
    towns_str = ", ".join(all_locations)

    # Cap exclusion list dumped into the prompt so it doesn't balloon over time.
    # The model uses these only as a "don't suggest these again" hint; the
    # authoritative dedup happens locally after the response is parsed.
    excluded_sample = sorted(excluded_domains)[:50]
    excluded_block = (
        "Avoid suggesting any company whose website domain matches one of "
        "these (already in our database):\n" + ", ".join(excluded_sample)
        if excluded_sample
        else "Our database is empty; any qualifying company is fair game."
    )

    region_label = PRIMARY_CITY if state_part else primary_city_name
    sample_town = SURROUNDING_TOWNS[0] if SURROUNDING_TOWNS else primary_city_name

    return f"""You are a B2B prospect researcher. Find {industry['count']} real companies that match ALL of these criteria:

1. Industry: {industry['name']}. {industry['guidance']}
2. Headquartered or with a primary office in the {region_label} metro area — specifically in or near one of: {towns_str}. EXCLUDE companies that only have a small satellite or sales office here; the company's main operations should be in the metro.
3. Employee count between {MIN_EMPLOYEES} and {MAX_EMPLOYEES} (inclusive). Use these cues to estimate:
   - Visible team/staff/leadership pages and their size
   - LinkedIn employee range if shown in search snippets
   - "Founded in" date and growth language
   - Number of office locations
   EXCLUDE: Fortune 500 firms, publicly traded companies, any company with more than {MAX_EMPLOYEES} employees nationally, any company that is clearly under {MIN_EMPLOYEES} employees (e.g. solo practitioners, 2-person shops). When in doubt about a borderline-large company, skip it.
4. The company must have a live website you have actually seen in search results. Do not invent companies. Do not guess domains.

Use the web_search tool to find these companies. Search for things like "{industry['name']} companies {primary_city_name}", "small {industry['name']} firms {sample_town} {state_part}", etc. Vary your searches across the listed towns to get geographic spread, not just the primary city.

{excluded_block}

When you have {industry['count']} qualifying companies, respond with ONLY a JSON array — no preamble, no markdown fences, no commentary. Each element must look like:

{{"company_name": "...", "website": "https://example.com", "city": "...", "employee_estimate": "approx 50" or "unknown", "headcount_verified": true or false}}

If after thorough searching you cannot find {industry['count']}, return whatever you did find (still valid JSON array) and the script will log the shortfall. Do not pad the list with companies you are not confident exist.
"""


def call_claude_for_industry(
    client: Anthropic,
    logger: logging.Logger,
    industry: dict,
    excluded_domains: set[str],
) -> tuple[list[dict], str | None]:
    """
    Returns (prospects, shortfall_reason). shortfall_reason is None on success.
    """
    prompt = build_prompt(industry, excluded_domains)

    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": WEB_SEARCH_MAX_USES,
    }]

    messages = [{"role": "user", "content": prompt}]
    response = None

    for attempt in range(1, MAX_API_ATTEMPTS + 1):
        try:
            logger.info(f"[{industry['name']}] API call attempt {attempt}")
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                tools=tools,
                messages=messages,
            )
            break
        except RateLimitError as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(f"[{industry['name']}] Rate limited: {e}. Sleeping {delay}s.")
            time.sleep(delay)
        except APIStatusError as e:
            if 500 <= e.status_code < 600 and attempt < MAX_API_ATTEMPTS:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(f"[{industry['name']}] API {e.status_code}: {e}. Retrying in {delay}s.")
                time.sleep(delay)
            else:
                logger.error(f"[{industry['name']}] API error: {e}")
                return [], f"API error: {e}"
        except APIError as e:
            logger.error(f"[{industry['name']}] Anthropic API error: {e}")
            if attempt == MAX_API_ATTEMPTS:
                return [], f"API error after {MAX_API_ATTEMPTS} attempts: {e}"
            time.sleep(RETRY_BASE_DELAY * attempt)
    else:
        return [], f"API exhausted {MAX_API_ATTEMPTS} attempts"

    if response is None:
        return [], "No response received from API"

    text_chunks = [
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    ]
    final_text = "\n".join(text_chunks).strip()

    if not final_text:
        return [], "Model returned no text content"

    prospects = parse_prospects_json(final_text, logger, industry['name'])

    if len(prospects) < industry['count']:
        reason = (
            f"Model returned {len(prospects)} of {industry['count']} requested "
            f"{industry['name']} prospects (possibly exhausted unique matches "
            f"or web_search hit its cap)."
        )
        return prospects, reason

    return prospects, None


def parse_prospects_json(text: str, logger: logging.Logger, industry_name: str) -> list[dict]:
    """
    The model is instructed to return a bare JSON array. Be defensive anyway:
    strip markdown fences and find the first '[' / last ']'.
    """
    cleaned = text.strip()

    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        logger.error(f"[{industry_name}] No JSON array found in response. Raw: {text[:500]}")
        return []

    candidate = cleaned[start : end + 1]

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        logger.error(f"[{industry_name}] JSON parse error: {e}. Candidate: {candidate[:500]}")
        return []

    if not isinstance(data, list):
        logger.error(f"[{industry_name}] JSON not a list: {type(data)}")
        return []

    cleaned_rows: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = (item.get("company_name") or "").strip()
        site = (item.get("website") or "").strip()
        if not name or not site:
            continue
        cleaned_rows.append({
            "company_name": name,
            "website": site,
            "city": (item.get("city") or "").strip(),
            "employee_estimate": (item.get("employee_estimate") or "").strip(),
            "headcount_verified": bool(item.get("headcount_verified", False)),
            "industry": industry_name,
        })

    return cleaned_rows


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------

def write_prospects(
    new_rows: list[dict],
    logger: logging.Logger,
) -> tuple[int, Path]:
    """
    Append to allprospects.csv (creating header if new) and write today's
    newprospects_MMDDYYYY.csv. Returns (rows_written, daily_path).
    """
    today_path = SCRIPT_DIR / f"newprospects_{datetime.now():%m%d%Y}.csv"

    all_exists = ALL_PROSPECTS_CSV.exists()
    with ALL_PROSPECTS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not all_exists:
            writer.writeheader()
        for row in new_rows:
            writer.writerow({"company_name": row["company_name"], "website": row["website"]})

    with today_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in new_rows:
            writer.writerow({"company_name": row["company_name"], "website": row["website"]})

    logger.info(f"Wrote {len(new_rows)} rows to {ALL_PROSPECTS_CSV.name} and {today_path.name}")
    return len(new_rows), today_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("Prospector run starting")
    logger.info(f"Model: {MODEL}")
    logger.info(
        f"Target: {TARGET_TOTAL} prospects in {PRIMARY_CITY}, "
        f"{MIN_EMPLOYEES}-{MAX_EMPLOYEES} employees, "
        f"industries: {', '.join(i['name'] for i in INDUSTRIES)}"
    )

    load_dotenv(SCRIPT_DIR / ".env")
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not found in .env or environment. Aborting.")
        return 1

    client = Anthropic(api_key=api_key)

    existing = load_existing_domains(ALL_PROSPECTS_CSV)
    logger.info(f"Loaded {len(existing)} existing domains from {ALL_PROSPECTS_CSV.name}")

    accepted: list[dict] = []
    accepted_domains_this_run: set[str] = set()
    shortfall_reasons: list[str] = []

    for i, industry in enumerate(INDUSTRIES):
        # Brief pause between industry calls to stay under per-minute rate caps.
        # Tune INTER_INDUSTRY_SLEEP at top of file if you keep hitting 429s.
        if i > 0:
            logger.info(f"Sleeping {INTER_INDUSTRY_SLEEP}s between industries to respect rate limits")
            time.sleep(INTER_INDUSTRY_SLEEP)

        # Combine permanent excluded domains with anything picked up this run
        excluded = existing | accepted_domains_this_run
        prospects, reason = call_claude_for_industry(
            client, logger, industry, excluded
        )

        if reason:
            shortfall_reasons.append(reason)
            logger.warning(reason)

        for p in prospects:
            domain = normalize_domain(p["website"])
            if not domain:
                logger.info(f"Skipping (no parseable domain): {p['company_name']} / {p['website']}")
                continue
            if domain in existing or domain in accepted_domains_this_run:
                logger.info(f"Skipping duplicate: {p['company_name']} ({domain})")
                continue

            accepted_domains_this_run.add(domain)
            accepted.append(p)

            verified_note = "" if p["headcount_verified"] else " [headcount unverified]"
            logger.info(
                f"Accepted [{industry['name']}]: {p['company_name']} ({domain}) — "
                f"{p['city']}, ~{p['employee_estimate']}{verified_note}"
            )

    rows_written, daily_path = write_prospects(accepted, logger)

    if rows_written < TARGET_TOTAL:
        shortfall_msg = (
            f"SHORTFALL: Target was {TARGET_TOTAL}, wrote {rows_written}. "
            f"Reasons: {'; '.join(shortfall_reasons) if shortfall_reasons else 'after dedup, too few unique new prospects remained.'}"
        )
        logger.warning(shortfall_msg)
    else:
        logger.info(f"Target met: {rows_written} prospects written.")

    logger.info("Prospector run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
