"""
Scheduled job runner.

Jobs:
  daily_scrape_all    – run all active scrapers
  daily_score_all     – re-score all properties
  daily_link_orphans  – link orphaned distress records to properties
  weekly_ghl_hot      – push hot leads to GHL
  hourly_foreclosure  – check for new foreclosure filings (high priority)

Usage:
  python -m jobs.runner --job daily_scrape_all
  python -m jobs.runner --job daily_score_all
  python -m jobs.runner --job scraper --source-key maricopa_recorder_foreclosure
"""
import argparse
import logging
import sys
import time
from datetime import datetime

from core.config import init_db_pool, get_cursor
from core.scoring import score_all_properties
from core.enrichment import link_orphaned_records

log = logging.getLogger("jobs")

# ──────────────────────────────────────────────────────────────
# Scraper registry
# ──────────────────────────────────────────────────────────────

SCRAPER_REGISTRY: dict = {}


def register_scrapers():
    """Lazy import to avoid circular deps."""
    global SCRAPER_REGISTRY
    from scrapers.maricopa.maypotenza_scraper import MayPotenzaTrusteeScraper
    from scrapers.maricopa.scrapers import (
        MaricopaRecorderScraper,
        MaricopaAssessorCSVScraper,
        MaricopaTaxScraper,
        MaricopaCodeViolationScraper,
    )
    from scrapers.pima.scrapers import (
        PimaRecorderScraper,
        PimaAssessorScraper,
        PimaTaxScraper,
        PimaProbateScraper,
    )

    SCRAPER_REGISTRY = {
        "maricopa_recorder_foreclosure": MaricopaRecorderScraper,
        "maypotenza_trustee_sales": MayPotenzaTrusteeScraper,
        "maricopa_assessor_csv": MaricopaAssessorCSVScraper,
        "maricopa_treasurer_tax": MaricopaTaxScraper,
        "maricopa_code_violations": MaricopaCodeViolationScraper,
        "pima_recorder_foreclosure": PimaRecorderScraper,
        "pima_assessor": PimaAssessorScraper,
        "pima_treasurer_tax": PimaTaxScraper,
        "pima_superior_court_probate": PimaProbateScraper,
    }
    return SCRAPER_REGISTRY


def run_scraper_by_key(source_key: str) -> dict:
    """Run a single scraper by source_key. Returns stats dict."""
    registry = register_scrapers()
    scraper_cls = registry.get(source_key)
    if not scraper_cls:
        log.error("Unknown source_key: %s", source_key)
        return {"error": f"Unknown source_key: {source_key}"}

    log.info("Starting scraper: %s", source_key)
    try:
        scraper = scraper_cls()
        stats = scraper.run()
        log.info("Completed %s: %s", source_key, stats)
        return stats
    except Exception as e:
        log.error("Scraper %s failed: %s", source_key, e, exc_info=True)
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────
# Job definitions
# ──────────────────────────────────────────────────────────────

def job_daily_scrape_all():
    """Run all active scrapers in priority order."""
    log.info("=== daily_scrape_all started ===")
    start = time.monotonic()

    register_scrapers()

    # Get active sources ordered by priority
    with get_cursor() as cur:
        cur.execute(
            "SELECT source_key FROM data_sources WHERE is_active = TRUE ORDER BY source_type"
        )
        keys = [row["source_key"] for row in cur.fetchall()]

    # Assessor first (foundation data), then distress signals
    priority_order = [
        "maricopa_assessor_csv",
        "pima_assessor",
        "maricopa_recorder_foreclosure",
        "pima_recorder_foreclosure",
        "pima_superior_court_probate",
        "maricopa_treasurer_tax",
        "pima_treasurer_tax",
        "maricopa_code_violations",
        "pima_dev_services_violations",
    ]
    ordered = [k for k in priority_order if k in keys]
    remaining = [k for k in keys if k not in ordered]
    all_keys = ordered + remaining

    results = {}
    for key in all_keys:
        if key not in SCRAPER_REGISTRY:
            log.debug("Skipping %s (no registered scraper)", key)
            continue
        results[key] = run_scraper_by_key(key)
        time.sleep(5)  # polite pause between scrapers

    elapsed = time.monotonic() - start
    log.info("=== daily_scrape_all done in %.1fs ===", elapsed)
    return results


def job_daily_score_all():
    """Re-score all properties after scraping."""
    log.info("=== daily_score_all started ===")
    count = score_all_properties()
    log.info("Scored %d properties", count)
    return {"scored": count}


def job_link_orphans():
    """Link orphaned distress records to property records."""
    log.info("=== link_orphans started ===")
    total = 0
    for county in ["Maricopa", "Pima"]:
        n = link_orphaned_records(county)
        total += n
    log.info("Linked %d orphaned records", total)
    return {"linked": total}


def job_push_hot_leads():
    """Push all hot leads to GHL webhook."""
    import requests as req_lib
    from core.config import settings
    import json

    log.info("=== push_hot_leads started ===")
    if not settings.GHL_WEBHOOK_URL:
        log.warning("GHL_WEBHOOK_URL not set — skipping")
        return {"skipped": True}

    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM properties WHERE lead_tier = 'hot' ORDER BY lead_score DESC"
        )
        props = cur.fetchall()

    pushed = 0
    errors = 0
    for prop in props:
        try:
            payload = {
                "type": "real_estate_distress_lead",
                "lead_tier": prop["lead_tier"],
                "lead_score": prop["lead_score"],
                "address": prop["address_full"],
                "county": prop["county"],
                "apn": prop["apn"],
                "owner_name": prop["owner_name"],
                "equity_est": float(prop["equity_est"] or 0),
                "equity_tier": prop["equity_tier"],
                "is_out_of_state": prop["is_out_of_state"],
            }
            headers = {"Content-Type": "application/json"}
            if settings.GHL_API_KEY:
                headers["Authorization"] = f"Bearer {settings.GHL_API_KEY}"

            resp = req_lib.post(
                settings.GHL_WEBHOOK_URL, json=payload, headers=headers, timeout=10
            )
            success = resp.status_code < 300

            with get_cursor() as cur2:
                cur2.execute(
                    """INSERT INTO ghl_webhook_log
                       (property_id, http_status, response_body, payload, success)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (str(prop["id"]), resp.status_code, resp.text[:1000],
                     json.dumps(payload), success),
                )
            if success:
                pushed += 1
            else:
                errors += 1

            time.sleep(0.2)  # 5 per second max
        except Exception as e:
            log.error("GHL push error for %s: %s", prop["id"], e)
            errors += 1

    log.info("GHL push: %d pushed, %d errors", pushed, errors)
    return {"pushed": pushed, "errors": errors}


def job_hourly_foreclosure():
    """High-priority: check for new foreclosure filings."""
    log.info("=== hourly_foreclosure check ===")
    register_scrapers()
    results = {}
    for key in ["maricopa_recorder_foreclosure", "pima_recorder_foreclosure"]:
        if key in SCRAPER_REGISTRY:
            results[key] = run_scraper_by_key(key)
    return results


# ──────────────────────────────────────────────────────────────
# Render.com cron entrypoint
# ──────────────────────────────────────────────────────────────

JOB_MAP = {
    "daily_scrape_all": job_daily_scrape_all,
    "daily_score_all": job_daily_score_all,
    "link_orphans": job_link_orphans,
    "push_hot_leads": job_push_hot_leads,
    "hourly_foreclosure": job_hourly_foreclosure,
}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(description="RE Distress Job Runner")
    parser.add_argument("--job", required=True, choices=list(JOB_MAP.keys()) + ["scraper"])
    parser.add_argument("--source-key", help="Source key for --job scraper")
    args = parser.parse_args()

    init_db_pool()

    if args.job == "scraper":
        if not args.source_key:
            print("--source-key required for --job scraper")
            sys.exit(1)
        result = run_scraper_by_key(args.source_key)
    else:
        fn = JOB_MAP[args.job]
        result = fn()

    print("Result:", result)
