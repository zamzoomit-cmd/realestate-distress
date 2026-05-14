"""
Bexar County (San Antonio) Texas Foreclosure Scraper
Pulls structured foreclosure data from the Bexar County Clerk's
public PDF list - updated monthly, no JavaScript needed.

Source: https://www.bexar.org/foreclosure-notices (public PDF)
Data: Document number, type, address, city, zip
"""
import json
import re
import logging
from datetime import date
from typing import Optional

import requests

from core.base_scraper import BaseScraper
from core.enrichment import find_or_create_property
from core.address_utils import normalize_apn
from core.config import get_cursor

log = logging.getLogger("scraper.bexar")


class BexarForeclosureScraper(BaseScraper):
    """
    Scrapes Bexar County Clerk's public monthly foreclosure list.
    Published as a structured PDF/text document — no JS required.
    """

    source_key = "bexar_foreclosure"
    county = "Bexar"
    doc_type = "foreclosure"
    STATE = "TX"

    # Bexar County Clerk public foreclosure PDF
    PDF_URL = "https://www.bexar.org/DocumentCenter/View/505/Current-County-Clerk-Foreclosures"
    # Backup: direct document center search
    BACKUP_URL = "https://www.bexar.org/foreclosure-notices"

    def fetch_records(self) -> list[dict]:
        records = []
        try:
            resp = self.get(self.PDF_URL)
            if resp.status_code == 200:
                text = resp.text
                records = self._parse_foreclosure_list(text)
                log.info("Bexar foreclosure PDF: found %d records", len(records))
            else:
                log.warning("Bexar PDF HTTP %d", resp.status_code)
        except Exception as e:
            log.error("Bexar scraper error: %s", e, exc_info=True)
        return records

    def _parse_foreclosure_list(self, text: str) -> list[dict]:
        """
        Parse the structured Bexar County foreclosure list.
        Format: DOCUMENT_NUMBER TYPE ADDRESS CITY/TOWN ZIP
        """
        records = []

        # Extract month/year from header
        month_match = re.search(r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{4})", text, re.I)
        sale_month = month_match.group(0) if month_match else ""

        # Parse each line
        lines = text.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Match pattern: DOC_NUMBER TYPE ADDRESS CITY ZIP
            # e.g. "20260600216 MORTGAGE 17020 RANCHO ESCONDIDO ATASCOSA 78002"
            m = re.match(
                r"^(\d{4}[A-Z]*\d+)\s+(MORTGAGE|TAX|HELOC|HOA)\s+(.+?)\s+([A-Z][A-Z\s]+)\s+(\d{5})$",
                line,
                re.IGNORECASE,
            )
            if m:
                doc_num = m.group(1)
                notice_type = m.group(2).upper()
                address = m.group(3).strip()
                city = m.group(4).strip()
                zip_code = m.group(5)

                full_address = f"{address}, {city}, TX {zip_code}"

                records.append({
                    "_source_url": self.PDF_URL,
                    "document_number": doc_num,
                    "notice_type": notice_type,
                    "property_address": address,
                    "city": city,
                    "zip": zip_code,
                    "address_full": full_address,
                    "sale_month": sale_month,
                    "state": self.STATE,
                })

        return records

    def get_doc_key(self, raw: dict) -> str:
        return raw.get("document_number", "")

    def get_source_url(self, raw: dict) -> str:
        doc = raw.get("document_number", "")
        return f"https://bexar.tx.publicsearch.us/search?department=RP&recordedDateRange=custom&term={doc}"

    def parse_record(self, raw: dict) -> Optional[dict]:
        doc_num = raw.get("document_number")
        if not doc_num:
            return None

        # Determine auction date - first Tuesday of next month
        auction_date = self._next_first_tuesday()

        return {
            "county": self.county,
            "state": self.STATE,
            "document_number": doc_num,
            "notice_type": raw.get("notice_type", "NTS"),
            "auction_date": auction_date,
            "property_address": raw.get("address_full"),
            "raw_data": raw,
        }

    def _next_first_tuesday(self) -> str:
        """Calculate the next first Tuesday of the month (TX auction date)."""
        from datetime import date, timedelta
        today = date.today()
        # Go to next month
        if today.month == 12:
            next_month = date(today.year + 1, 1, 1)
        else:
            next_month = date(today.year, today.month + 1, 1)

        # Find first Tuesday (weekday 1)
        day = next_month
        while day.weekday() != 1:
            day += timedelta(days=1)
        return day.isoformat()

    def store_record(self, parsed: dict) -> str:
        address = parsed.get("property_address")
        property_id = find_or_create_property(
            county=self.county,
            apn=None,
            address_raw=address,
            source_id=parsed.get("source_id"),
            source_url=parsed.get("source_url"),
            extra_fields={"state": self.STATE},
        )

        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO foreclosure_events
                  (county, document_number, property_id, source_id, source_doc_id,
                   source_url, notice_type, auction_date, property_address, raw_data)
                VALUES
                  (%(county)s, %(document_number)s, %(property_id)s, %(source_id)s,
                   %(source_doc_id)s, %(source_url)s, %(notice_type)s, %(auction_date)s,
                   %(property_address)s, %(raw_data)s::jsonb)
                ON CONFLICT (county, document_number) DO UPDATE SET
                   auction_date = EXCLUDED.auction_date,
                   property_id = EXCLUDED.property_id,
                   updated_at = NOW()
                """,
                {
                    **parsed,
                    "property_id": property_id,
                    "raw_data": json.dumps(parsed.get("raw_data", {})),
                },
            )
        return "new"
