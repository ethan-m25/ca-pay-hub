#!/usr/bin/env python3
"""
ca-pay-hub/scripts/search-lever.py
Lever job board scraper — California edition.

CA Labor Code §432.3: employers with 15+ employees must include pay scale
in all job postings. Effective January 1, 2023.

Strategy:
  1. Seed slugs (known CA-present Lever companies) + Exa discovery
  2. Lever public postings JSON API → all jobs per company (no auth needed)
  3. Salary: structured salaryRange field first, then text regex fallback
  4. CA filter: location mentions California / Bay Area / LA / remote-eligible

Run: python3 ~/ca-pay-hub/scripts/search-lever.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, exa_search, load_existing_keys,
    load_existing_urls,
    write_job, TODAY, OUTPUT_FILE,
)

from scrapling import Fetcher

LOG_FILE  = os.path.expanduser("~/ca-pay-hub/scripts/lever.log")
LOCK_FILE = os.path.expanduser("~/ca-pay-hub/scripts/.lever.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)
fetcher = Fetcher()

# ── Seed slugs — CA-present Lever companies ───────────────────────────────────
# === Phase 4 seed loader (added 2026-05-27) ===
sys.path.insert(0, os.path.expanduser('~/shared-scripts'))
from hub_employer_seeds import load_lever_seeds
SEED_SLUGS = load_lever_seeds('ca')

DISCOVERY_QUERIES = [
    'site:jobs.lever.co "San Francisco" OR "SF" salary 2026',
    'site:jobs.lever.co "Los Angeles" OR "LA" salary range 2026',
    'site:jobs.lever.co "California" OR ", CA" salary "$" 2026',
    'site:jobs.lever.co "Bay Area" OR "Silicon Valley" salary annual 2026',
    'site:jobs.lever.co "San Jose" OR "Palo Alto" OR "Mountain View" salary 2026',
    'site:jobs.lever.co California tech OR biotech OR fintech salary 2026',
    'site:jobs.lever.co "San Diego" OR "Orange County" salary range 2026',
]

CA_TERMS = [
    "california", "san francisco", "sf,", "sf ", "los angeles", "la,", "la ",
    "san jose", "palo alto", "mountain view", "menlo park", "redwood city",
    "santa clara", "sunnyvale", "cupertino", "fremont", "oakland", "berkeley",
    "emeryville", "san mateo", "burlingame", "san diego", "santa monica",
    "culver city", "hollywood", "venice", "pasadena", "irvine", "anaheim",
    "long beach", "sacramento", "san francisco bay area", "bay area",
    "silicon valley", "south bay", "east bay", ", ca", "ca,",
    "remote",   # remote roles from CA-based companies under §432.3
]

_NON_CA_LOC_TERMS = [
    "new york", "nyc", ", ny,", "seattle", ", wa,", "chicago", "boston",
    "austin", "texas", ", tx,", "florida", ", fl,", "denver", "colorado", ", co,",
    "washington, dc", "toronto", "ontario", "british columbia",
    "london, uk", "london, england",
]

SALARY_RE = [
    re.compile(r'\$\s*([\d,]+)(?:\.\d+)?\s*(?:USD|usd)?\s*[-–—to]+\s*\$\s*([\d,]+)', re.IGNORECASE),
    re.compile(r'\$([\d]+(?:\.\d+)?)[kK]\s*[-–—]\s*\$([\d]+(?:\.\d+)?)[kK]', re.IGNORECASE),
    re.compile(r'(?:pay|salary|compensation|base|wage|range)[^$\n]{0,50}\$?([\d,]{5,})\s*[-–—to]+\s*\$?([\d,]{5,})', re.IGNORECASE),
]

LEVER_SLUG_RE = re.compile(r'https?://jobs\.lever\.co/([a-zA-Z0-9._-]+)', re.IGNORECASE)
_SKIP_SLUGS = {'jobs', 'search', 'home'}


def discover_slugs(seed_slugs):
    known = set(seed_slugs)
    discovered = set()
    for i, query in enumerate(DISCOVERY_QUERIES, 1):
        log(f"  Discovery Exa [{i}/{len(DISCOVERY_QUERIES)}]: {query[:60]}...")
        resp = exa_search(query, num_results=15, start_date=LOOKBACK_DATE, log=log)
        if not resp:
            continue
        new = 0
        for r in resp.get("results", []):
            m = LEVER_SLUG_RE.search(r.get("url", ""))
            if not m:
                continue
            slug = m.group(1).lower()
            if slug in _SKIP_SLUGS or slug in known or len(slug) < 2:
                continue
            discovered.add(slug)
            new += 1
        log(f"    → {new} new slugs")
        time.sleep(1.5)
    return discovered


def fetch_company_jobs(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        page = fetcher.get(url, timeout=20)
        if page.status != 200:
            return []
        return page.json() or []
    except Exception as e:
        log(f"  API error ({slug}): {e}")
        return []


def is_california(location_str, desc_text=""):
    loc = (location_str or "").lower()
    if any(t in loc for t in _NON_CA_LOC_TERMS):
        return False
    if any(t in loc for t in CA_TERMS):
        return True
    if "remote" in loc and not any(t in loc for t in _NON_CA_LOC_TERMS):
        return True
    return False


def parse_location(location_str):
    loc = (location_str or "").lower()
    city_map = {
        "san francisco": "San Francisco, CA", "sf,": "San Francisco, CA",
        "los angeles": "Los Angeles, CA", "santa monica": "Santa Monica, CA",
        "palo alto": "Palo Alto, CA", "mountain view": "Mountain View, CA",
        "menlo park": "Menlo Park, CA", "san jose": "San Jose, CA",
        "sunnyvale": "Sunnyvale, CA", "santa clara": "Santa Clara, CA",
        "redwood city": "Redwood City, CA", "san mateo": "San Mateo, CA",
        "emeryville": "Emeryville, CA", "oakland": "Oakland, CA",
        "berkeley": "Berkeley, CA", "cupertino": "Cupertino, CA",
        "san diego": "San Diego, CA", "irvine": "Irvine, CA",
        "sacramento": "Sacramento, CA", "culver city": "Culver City, CA",
        "pasadena": "Pasadena, CA",
    }
    for key, label in city_map.items():
        if key in loc:
            return label
    if "remote" in loc:
        return "Remote (CA)"
    return "California, CA"


def extract_salary_from_range(sal_range):
    if not sal_range:
        return None
    currency = sal_range.get("currency", "").upper()
    if currency not in ("USD", ""):
        return None
    if sal_range.get("interval", "") != "per-year-salary":
        return None
    try:
        vmin = int(float(sal_range["min"]))
        vmax = int(float(sal_range["max"]))
        if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
            return vmin, vmax
    except (KeyError, ValueError, TypeError):
        pass
    return None


def extract_salary_from_text(text):
    if not text:
        return None
    clean = html_mod.unescape(re.sub(r'<[^>]+>', ' ', text))
    clean = html_mod.unescape(re.sub(r'\s+', ' ', clean).strip())
    for pat in SALARY_RE:
        m = pat.search(clean)
        if m:
            try:
                raw_min = m.group(1).replace(",", "")
                raw_max = m.group(2).replace(",", "")
                if "k" in m.group(0).lower():
                    vmin = int(float(raw_min) * 1000)
                    vmax = int(float(raw_max) * 1000)
                else:
                    vmin = int(float(raw_min))
                    vmax = int(float(raw_max))
                if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
                    return vmin, vmax
            except (ValueError, IndexError):
                continue
    return None


def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log("=== CA Lever scraper started ===")
    log(f"Output: {OUTPUT_FILE}")

    log(f"Running Exa discovery ({len(DISCOVERY_QUERIES)} queries)...")
    extra_slugs = discover_slugs(SEED_SLUGS)
    log(f"  {len(SEED_SLUGS)} seed + {len(extra_slugs)} discovered = "
        f"{len(SEED_SLUGS) + len(extra_slugs)} total slugs")

    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    seen_urls = load_existing_urls()
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    total_found = 0
    api_failures = 0
    discovered_slug_yield = {}
    all_slugs = list(SEED_SLUGS) + sorted(extra_slugs)

    for slug in all_slugs:
        jobs = fetch_company_jobs(slug)
        if not jobs:
            api_failures += 1
            time.sleep(1)
            continue

        company_name = slug.replace("-", " ").replace("_", " ").replace(".", " ").title()
        log(f"\n── {company_name} ({slug}): {len(jobs)} postings ──")
        ca_count = 0
        found_this = 0

        for job in jobs:
            cats = job.get("categories") or {}
            loc_name = cats.get("location", "") or cats.get("allLocations", "")
            if isinstance(loc_name, list):
                loc_name = ", ".join(loc_name)

            desc_plain = job.get("descriptionPlain") or ""
            if not is_california(loc_name, desc_plain):
                continue
            ca_count += 1

            title = (job.get("text") or "").strip()
            if not title:
                continue

            key = f"{title.lower()}|{company_name.lower()}"
            if key in seen_keys:
                continue

            salary = extract_salary_from_range(job.get("salaryRange"))
            if not salary:
                sal_desc = job.get("salaryDescriptionPlain") or job.get("salaryDescription") or ""
                salary = extract_salary_from_text(sal_desc) or extract_salary_from_text(desc_plain)

            if not salary:
                log(f"  [{title[:50]}] → no salary")
                continue

            vmin, vmax = salary
            job_id = job.get("id", "")
            abs_url = f"https://jobs.lever.co/{slug}/{job_id}" if job_id else ""
            if abs_url and abs_url in seen_urls:
                continue

            posted = TODAY
            created_ms = job.get("createdAt")
            if created_ms:
                try:
                    posted = datetime.fromtimestamp(
                        int(created_ms) / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d")
                except (ValueError, OSError):
                    pass

            job_out = {
                "role":            title,
                "company":         company_name,
                "min":             vmin,
                "max":             vmax,
                "location":        parse_location(loc_name),
                "source_url":      abs_url,
                "posted":          posted,
                "source_platform": "lever",
            }

            write_job(OUTPUT_FILE, job_out)
            seen_keys.add(key)
            seen_urls.add(abs_url)
            total_found += 1
            found_this += 1
            log(f"  FOUND: {title[:50]} | ${vmin:,}–${vmax:,} [{loc_name}]")

        log(f"  CA: {ca_count} | New w/ salary: {found_this}")
        if slug in extra_slugs:
            discovered_slug_yield[slug] = found_this
        time.sleep(2)

    log(f"\n=== CA Lever scraper complete: {total_found} new jobs (api_failures={api_failures}) ===")

    seed_set = set(SEED_SLUGS)
    newly_qualified = {
        slug: count for slug, count in discovered_slug_yield.items()
        if slug not in seed_set and count >= 2
    }
    if newly_qualified:
        log(f"\nAuto-injecting {len(newly_qualified)} high-yield slug(s) into SEED_SLUGS:")
        script_path = os.path.abspath(__file__)
        try:
            source = open(script_path).read()
            new_lines = []
            for slug, count in sorted(newly_qualified.items(), key=lambda x: -x[1]):
                if f'"{slug}"' in source:
                    continue
                log(f"  + {slug} ({count} CA+salary jobs)")
                new_lines.append(f'    "{slug}",  # auto-discovered {TODAY} — {count} CA+salary')
            if new_lines:
                marker = "]\n\nDISCOVERY_QUERIES"
                source = source.replace(marker, "\n" + "\n".join(new_lines) + "\n" + marker)
                open(script_path, "w").write(source)
        except Exception as e:
            log(f"  Auto-inject error: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
