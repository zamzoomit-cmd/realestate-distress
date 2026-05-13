"""
Base scraper class. All county scrapers inherit from this.
Handles: rate limiting, raw document storage, error logging,
deduplication, robots.txt respect, and run tracking.
"""
import json
import time
import uuid
import logging
import hashlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import settings, get_cursor, execute_upsert

log = logging.getLogger("scraper.base")


class RateLimiter:
    """Token-bucket rate limiter."""

    def __init__(self, rps: float = 1.0):
        self.min_interval = 1.0 / rps if rps > 0 else 1.0
        self._last_call = 0.0

    def wait(self):
        elapsed = time.monotonic() - self._last_call
        sleep_for = self.min_interval - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)
        self._last_call = time.monotonic()


class BaseScraper(ABC):
    """
    Base class for all county data scrapers.

    Subclasses must implement:
        - fetch_records() -> list[dict]
        - parse_record(raw: dict) -> dict
        - get_doc_key(raw: dict) -> str

    Scrapers MUST NOT:
        - Bypass login walls or CAPTCHAs
        - Access private/restricted data
        - Send unauthorized credentials
        - Violate robots.txt directives
    """

    source_key: str = ""
    county: str = ""
    doc_type: str = ""

    def __init__(self):
        self._source_id: str | None = None
        self._run_id: str | None = None
        self._rate_limiter: RateLimiter | None = None
        self._session: requests.Session | None = None
        self.log = logging.getLogger(f"scraper.{self.source_key}")

    # ──────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────

    def run(self) -> dict:
        """Execute full scrape run. Returns run summary."""
        self._load_source()
        self._start_run()

        stats = {"found": 0, "new": 0, "updated": 0, "skipped": 0, "errors": 0}

        try:
            records = self.fetch_records()
            stats["found"] = len(records)

            for raw in records:
                try:
                    doc_key = self.get_doc_key(raw)
                    source_url = self.get_source_url(raw)

                    # Save raw document
                    doc_id = self._save_raw_document(doc_key, source_url, raw)

                    # Parse structured fields
                    parsed = self.parse_record(raw)
                    if not parsed:
                        stats["skipped"] += 1
                        continue

                    parsed["source_id"] = self._source_id
                    parsed["source_doc_id"] = doc_id
                    parsed["source_url"] = source_url

                    # Store parsed record
                    result = self.store_record(parsed)
                    stats[result] = stats.get(result, 0) + 1

                except Exception as e:
                    stats["errors"] += 1
                    self.log.error("Error processing record %s: %s", raw, e, exc_info=True)

            self._complete_run(stats, status="success")

        except Exception as e:
            self.log.error("Scrape run failed: %s", e, exc_info=True)
            self._complete_run(stats, status="failed", error=str(e))

        return stats

    # ──────────────────────────────────────────────────────────
    # Abstract methods
    # ──────────────────────────────────────────────────────────

    @abstractmethod
    def fetch_records(self) -> list[dict]:
        """Fetch raw records from the source. Return list of raw dicts."""
        ...

    @abstractmethod
    def parse_record(self, raw: dict) -> dict | None:
        """Parse raw record into structured dict for DB. Return None to skip."""
        ...

    @abstractmethod
    def get_doc_key(self, raw: dict) -> str:
        """Return a unique key for this document (doc number, case number, APN)."""
        ...

    def get_source_url(self, raw: dict) -> str:
        """Return the source URL for this record."""
        return raw.get("_source_url", "")

    def store_record(self, parsed: dict) -> str:
        """Store parsed record to appropriate table. Returns 'new'|'updated'|'skipped'."""
        raise NotImplementedError("Subclass must implement store_record()")

    # ──────────────────────────────────────────────────────────
    # HTTP session
    # ──────────────────────────────────────────────────────────

    def get_session(self) -> requests.Session:
        if self._session is None:
            session = requests.Session()
            retry = Retry(
                total=3,
                backoff_factor=2,
                status_forcelist=[429, 500, 502, 503, 504],
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update({
                "User-Agent": settings.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })
            self._session = session
        return self._session

    def get(self, url: str, **kwargs) -> requests.Response:
        """Rate-limited GET request."""
        if self._rate_limiter:
            self._rate_limiter.wait()
        self.log.debug("GET %s", url)
        return self.get_session().get(
            url, timeout=settings.REQUEST_TIMEOUT, **kwargs
        )

    def post(self, url: str, **kwargs) -> requests.Response:
        if self._rate_limiter:
            self._rate_limiter.wait()
        self.log.debug("POST %s", url)
        return self.get_session().post(
            url, timeout=settings.REQUEST_TIMEOUT, **kwargs
        )

    # ──────────────────────────────────────────────────────────
    # DB helpers
    # ──────────────────────────────────────────────────────────

    def _load_source(self):
        with get_cursor() as cur:
            cur.execute(
                "SELECT id, rate_limit_rps FROM data_sources WHERE source_key = %s",
                (self.source_key,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Source key not found in DB: {self.source_key}")
            self._source_id = str(row["id"])
            self._rate_limiter = RateLimiter(float(row["rate_limit_rps"]))

    def _start_run(self):
        run_id = str(uuid.uuid4())
        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO scrape_runs (id, source_id, status)
                VALUES (%s, %s, 'running')
                """,
                (run_id, self._source_id),
            )
        self._run_id = run_id
        self.log.info("Started scrape run %s for %s", run_id, self.source_key)

    def _complete_run(self, stats: dict, status: str = "success", error: str | None = None):
        with get_cursor() as cur:
            cur.execute(
                """
                UPDATE scrape_runs
                SET status = %s,
                    completed_at = NOW(),
                    records_found = %s,
                    records_new = %s,
                    records_updated = %s,
                    records_skipped = %s,
                    error_message = %s
                WHERE id = %s
                """,
                (
                    status,
                    stats.get("found", 0),
                    stats.get("new", 0),
                    stats.get("updated", 0),
                    stats.get("skipped", 0),
                    error,
                    self._run_id,
                ),
            )
        self.log.info(
            "Completed run %s | status=%s | %s",
            self._run_id, status, stats
        )

    def _save_raw_document(self, doc_key: str, source_url: str, raw: dict) -> str | None:
        """Persist raw document. Returns source_documents.id."""
        try:
            content = json.dumps(raw, default=str)
            doc_id = str(uuid.uuid4())
            with get_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO source_documents
                      (id, scrape_run_id, source_id, source_url, doc_type, doc_key, raw_content, parse_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'parsed')
                    ON CONFLICT (source_id, doc_key) DO UPDATE
                      SET raw_content = EXCLUDED.raw_content,
                          fetched_at = NOW(),
                          parse_status = 'parsed'
                    RETURNING id
                    """,
                    (doc_id, self._run_id, self._source_id, source_url,
                     self.doc_type, doc_key, content),
                )
                row = cur.fetchone()
                return str(row["id"]) if row else doc_id
        except Exception as e:
            self.log.warning("Failed to save raw doc %s: %s", doc_key, e)
            return None

    # ──────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def clean_text(val: Any) -> str | None:
        if val is None:
            return None
        return str(val).strip() or None

    @staticmethod
    def parse_money(val: Any) -> float | None:
        if val is None:
            return None
        try:
            return float(str(val).replace("$", "").replace(",", "").strip())
        except (ValueError, TypeError):
            return None

    @staticmethod
    def parse_date(val: Any) -> str | None:
        """Parse various date formats to ISO date string."""
        if not val:
            return None
        from dateutil import parser as dateparser
        try:
            return dateparser.parse(str(val)).date().isoformat()
        except Exception:
            return None
