#!/usr/bin/env python3
"""
ca-pay-hub/scripts/search-greenhouse.py
Greenhouse job board scraper — California edition.

CA Labor Code §432.3 requires pay scale disclosure on all job postings.
Silicon Valley and LA tech companies are heavily on Greenhouse.

Run: python3 ~/ca-pay-hub/scripts/search-greenhouse.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE, CA_TERMS,
)

from scrapling import Fetcher

LOG_FILE  = os.path.expanduser("~/ca-pay-hub/scripts/greenhouse.log")
LOCK_FILE = os.path.expanduser("~/ca-pay-hub/scripts/.greenhouse.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)
fetcher = Fetcher()

SEED_SLUGS = [
    # ── SF / Bay Area Flagship Tech ───────────────────────────────────────────
    ("google", None),              # Google, Mountain View HQ
    ("meta", None),                # Meta, Menlo Park HQ
    ("apple", None),               # Apple, Cupertino HQ
    ("salesforce", None),          # Salesforce, SF HQ
    ("airbnb", None),              # Airbnb, SF HQ
    ("lyft", None),                # Lyft, SF HQ
    ("uber", None),                # Uber, SF HQ
    ("doordash", None),            # DoorDash, SF HQ
    ("dropbox", None),             # Dropbox, SF HQ
    ("github", None),              # GitHub, SF HQ
    ("stripe", None),              # Stripe, SF HQ
    ("square", None),              # Block/Square, SF HQ
    ("databricks", None),          # Databricks, SF HQ
    ("figma", None),               # Figma, SF HQ
    ("asana", None),               # Asana, SF HQ
    ("zendesk", None),             # Zendesk, SF HQ
    ("twilio", None),              # Twilio, SF HQ
    ("okta", None),                # Okta, SF HQ
    ("cloudflare", None),          # Cloudflare, SF HQ
    ("splunk", None),              # Splunk, SF HQ
    ("pagerduty", None),           # PagerDuty, SF HQ
    ("mixpanel", None),            # Mixpanel, SF HQ
    ("amplitude", None),           # Amplitude, SF HQ
    # ── Silicon Valley ────────────────────────────────────────────────────────
    ("nvidia", None),              # NVIDIA, Santa Clara HQ
    ("intel", None),               # Intel, Santa Clara HQ
    ("adobe", None),               # Adobe, San Jose HQ
    ("zoom", None),                # Zoom, San Jose HQ
    ("servicenow", None),          # ServiceNow, Santa Clara
    ("paloaltonetworks", None),    # Palo Alto Networks, Santa Clara
    ("fortinet", None),            # Fortinet, Sunnyvale
    ("linkedin", None),            # LinkedIn, Sunnyvale HQ
    ("intuit", None),              # Intuit, Mountain View
    ("hp", None),                  # HP, Palo Alto
    ("vmware", None),              # VMware, Palo Alto
    ("arista", None),              # Arista Networks, Santa Clara
    # ── LA / Southern California ──────────────────────────────────────────────
    ("snap", None),                # Snap Inc., Santa Monica HQ
    ("tiktok", None),              # TikTok, Culver City
    ("riotgames", None),           # Riot Games, LA HQ
    ("activision", None),          # Activision, Santa Monica
    ("hulu", None),                # Hulu, Santa Monica HQ
    ("netsuite", None),            # NetSuite (Oracle), LA
    ("hawkeyeinnovations", None),  # Hawkeye, LA
    # ── Startups / Growth ─────────────────────────────────────────────────────
    ("anthropic", None),           # Anthropic, SF HQ
    ("openai", None),              # OpenAI, SF HQ
    ("scale", None),               # Scale AI, SF HQ
    ("brex", None),                # Brex, SF HQ
    ("gusto", None),               # Gusto, SF HQ
    ("rippling", None),            # Rippling, SF HQ
    ("lattice", None),             # Lattice, SF HQ
    ("notion", None),              # Notion, SF HQ
    ("airtable", None),            # Airtable, SF HQ
    ("retool", None),              # Retool, SF HQ
    ("linear", None),              # Linear, SF HQ
    ("vercel", None),              # Vercel, SF HQ
    ("loom", None),                # Loom, SF HQ
    ("mercury", None),             # Mercury, SF HQ
    ("ramp", None),                # Ramp, SF office
    # ── Healthcare / Biotech ──────────────────────────────────────────────────
    ("genentech", None),           # Genentech, South SF
    ("gilead", None),              # Gilead Sciences, Foster City
    ("illumina", None),            # Illumina, San Diego
    ("qualcomm", None),            # Qualcomm, San Diego
    ("gen", None),                 # Gen Digital (Norton), Tempe/CA
    # ── E-commerce / Consumer ─────────────────────────────────────────────────
    ("netflix", None),             # Netflix, Los Gatos HQ
    ("shopify", None),             # Shopify, remote/CA
    ("pinterest", None),           # Pinterest, SF HQ
    ("yelp", None),                # Yelp, SF HQ
    ("eventbrite", None),          # Eventbrite, SF HQ
    ("stitch-fix", None),          # Stitch Fix, SF HQ
    # ── Infrastructure / DevOps ───────────────────────────────────────────────
    ("hashicorp", None),           # HashiCorp, SF HQ
    ("docker", None),              # Docker, San Mateo
    ("confluent", None),           # Confluent, Mountain View
    ("snowflake", None),           # Snowflake, San Mateo
    ("datadog", None),             # Datadog, SF
    ("newrelic", None),            # New Relic, SF HQ
    ("dynatrace", None),           # Dynatrace, Waltham/CA
]


SALARY_PATTERNS = [
    r'\$\s*([\d,]+)\s*[-–—]\s*\$\s*([\d,]+)',
    r'([\d,]+)\s*[-–—]\s*([\d,]+)\s*(?:USD|usd)',
    r'salary[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'compensation[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'pay range[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'pay scale[:\s]+\$?([\d,]+)[kK]?\s*[-–—]\s*\$?([\d,]+)[kK]?',
    r'"salary_min":\s*(\d+).*?"salary_max":\s*(\d+)',
    r'"min_salary":\s*(\d+).*?"max_salary":\s*(\d+)',
]


def parse_salary_from_text(text: str):
    if not text:
        return None, None
    text = html_mod.unescape(html_mod.unescape(text))
    for pat in SALARY_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                raw_min = m.group(1).replace(",", "")
                raw_max = m.group(2).replace(",", "")
                val_min = int(float(raw_min))
                val_max = int(float(raw_max))
                if val_min < 1000:
                    val_min *= 1000
                if val_max < 1000:
                    val_max *= 1000
                if 30_000 <= val_min < val_max <= 1_500_000:
                    return val_min, val_max
            except (ValueError, IndexError):
                continue
    return None, None


def is_ca_job(title: str, location: str, content: str) -> bool:
    combined = f"{title} {location} {content}".lower()
    return any(term in combined for term in CA_TERMS)


def fetch_company_jobs(slug: str, company_name_override=None):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        resp = fetcher.get(url, timeout=20)
        data = resp.json()
    except Exception as e:
        log(f"  [{slug}] API error: {e}")
        return []

    jobs_raw = data.get("jobs", [])
    if not jobs_raw:
        return []

    company_name = company_name_override or data.get("company", {}).get("name") or slug.title()
    results = []

    for j in jobs_raw:
        updated_at = j.get("updated_at", "")
        if updated_at and updated_at < LOOKBACK_DATE:
            continue

        title = j.get("title", "").strip()
        location_obj = j.get("location", {})
        location = location_obj.get("name", "") if isinstance(location_obj, dict) else str(location_obj)
        content_html = j.get("content", "")
        content_text = re.sub(r'<[^>]+>', ' ', content_html)
        content_text = html_mod.unescape(content_text)

        if not is_ca_job(title, location, content_text):
            continue

        val_min, val_max = parse_salary_from_text(content_html + " " + content_text)
        if val_min is None:
            val_min, val_max = parse_salary_from_text(str(j))

        if val_min is None:
            continue

        posted_date = updated_at[:10] if updated_at else TODAY
        job_url = j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j.get('id','')}"

        results.append({
            "role": title,
            "company": company_name,
            "min": val_min,
            "max": val_max,
            "location": location or "San Francisco, CA",
            "source_url": job_url,
            "posted": posted_date,
            "source_platform": "greenhouse",
        })

    return results


def main():
    if not acquire_lock(LOCK_FILE, log):
        return

    log("=== CA Greenhouse scraper started ===")
    existing = load_existing_keys()
    log(f"Existing dedup keys: {len(existing)}")

    new_count = 0
    for slug, name_override in SEED_SLUGS:
        log(f"[{slug}] fetching...")
        jobs = fetch_company_jobs(slug, name_override)
        for job in jobs:
            key = f"{job['role'].lower().strip()}|{job['company'].lower().strip()}"
            if key in existing:
                continue
            write_job(OUTPUT_FILE, job)
            existing.add(key)
            new_count += 1
            log(f"  + {job['role']} @ {job['company']} | ${job['min']:,}–${job['max']:,} | {job['location']}")
        time.sleep(0.5)

    log(f"=== Done. {new_count} new CA jobs written to {OUTPUT_FILE} ===")


if __name__ == "__main__":
    main()
