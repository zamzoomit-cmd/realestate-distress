"""
Dallas County Texas Foreclosure Scraper
Pulls foreclosure notices from Dallas County Clerk's public website.
PDFs are organized by city and month - direct download links.

Source: https://www.dallascounty.org/government/county-clerk/recording/foreclosures.php
"""
import json
import re
import logging
import time
from datetime import date, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

from core.base_scraper import BaseScraper
from core.enrichment import find_or_create_property
from core.config import get_cursor

log = logging.getLogger("scraper.dallas")


class DallasForeclosureScraper(BaseScraper):
    """
    Scrapes Dallas County Clerk's public foreclosure PDF list.
    Downloads and parses each PDF to extract property addresses,
    borrower names, loan amounts, and auction dates.
    """

    source_key = "dallas_foreclosure"
    county = "Dallas"
    doc_type = "foreclosure"
    STATE = "TX"

    BASE_URL = "https://www.dallascounty.org"
    FORECLOSURE_PAGE = "https://www.dallascounty.org/government/county-clerk/recording/foreclosures.php"
    MEDIA_BASE = "https://www.dallascounty.org/department/countyclerk/media/foreclosure"

    def fetch_records(self) -> list[dict]:
        records = []
        try:
            # Get the foreclosure page to find current month PDFs
            resp = self.get(self.FORECLOSURE_PAGE)
            if resp.status_code != 200:
                log.warning("Dallas foreclosure page HTTP %d", resp.status_code)
                return []

            # Find all PDF links for current and next month
            soup = BeautifulSoup(resp.text, "html.parser")
            pdf_links = self._extract_pdf_links(soup)
            log.info("Dallas: found %d PDF files", len(pdf_links))

            # Download and parse each PDF
            for pdf_url in pdf_links[:20]:  # Limit to first 20 for rate limiting
                try:
                    self._rate_limiter.wait()
                    pdf_resp = self.get(pdf_url)
                    if pdf_resp.status_code == 200:
                        city = self._city_from_url(pdf_url)
                        parsed = self._parse_pdf_text(pdf_resp.text, pdf_url, city)
                        records.extend(parsed)
                        log.info("Dallas PDF %s: %d records", city, len(parsed))
                    time.sleep(1)
                except Exception as e:
                    log.error("Error parsing PDF %s: %s", pdf_url, e)

        except Exception as e:
            log.error("Dallas scraper error: %s", e, exc_info=True)

        log.info("Dallas total: %d foreclosure records", len(records))
        return records

    def _extract_pdf_links(self, soup: BeautifulSoup) -> list[str]:
        """Extract PDF links for current month from the page."""
        links = []
        current_month = date.today().strftime("%B")  # e.g. "May"
        next_month_date = date.today().replace(day=1)
        if next_month_date.month == 12:
            next_month = "January"
        else:
            from calendar import month_name
            next_month = month_name[next_month_date.month + 1]

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower():
                # Get full URL
                if href.startswith("http"):
                    full_url = href
                else:
                    full_url = self.BASE_URL + href

                # Check if it's for current or next month
                for month in [current_month, next_month, "June", "July"]:
                    if f"/{month}/" in full_url or f"/{month.lower()}/" in full_url.lower():
                        links.append(full_url)
                        break

        # If no links found from page, try direct media URLs for next month
        if not links:
            next_month_name = self._next_sale_month_name()
            for city in ["Dallas", "Garland", "Irving", "Mesquite", "Grand-Prairie",
                         "DeSoto", "Cedar-Hill", "Lancaster", "Carrollton", "Richardson"]:
                for suffix in ["_1", "_2", "_3", "_4"]:
                    url = f"{self.MEDIA_BASE}/{next_month_name}/{city}{suffix}.pdf"
                    links.append(url)

        return links

    def _next_sale_month_name(self) -> str:
        """Get the name of the next foreclosure sale month."""
        from calendar import month_name
        today = date.today()
        next_month = today.month + 1 if today.month < 12 else 1
        return month_name[next_month]

    def _city_from_url(self, url: str) -> str:
        """Extract city name from PDF URL."""
        parts = url.split("/")
        if parts:
            filename = parts[-1].replace(".pdf", "")
            # Remove trailing _1, _2 etc
            city = re.sub(r"_\d+.*$", "", filename)
            return city.replace("-", " ").title()
        return "Unknown"

    def _parse_pdf_text(self, text: str, source_url: str, city: str) -> list[dict]:
        """
        Parse Dallas County foreclosure PDF text.
        Each notice contains: borrower name, address, legal description,
        trustee info, loan amount, sale date.
        """
        records = []

        # Split by notice boundaries - each notice starts with a T.S. # or similar
        # Look for property addresses using Texas address pattern
        address_pattern = re.compile(
            r"(?:commonly known as|property address|located at)[:\s]+([0-9]+\s+[A-Z0-9\s,\.]+(?:ST|AVE|BLVD|DR|RD|LN|CT|CIR|WAY|PKWY|HWY)[A-Z\s,\.]*(?:DALLAS|GARLAND|IRVING|MESQUITE|GRAND PRAIRIE|DESOTO|CEDAR HILL|LANCASTER|CARROLLTON|RICHARDSON|ROWLETT|DUNCANVILLE|BALCH SPRINGS)[,\s]*TEXAS?\s*\d{5})",
            re.IGNORECASE
        )

        # Alternative: look for "commonly known as" addresses
        simple_addr = re.compile(
            r"(?:commonly known as|Commonly known as)[:\s]*([0-9]+[^,\n]+(?:TX|Texas)[,\s]*\d{5})",
            re.IGNORECASE
        )

        # Extract trustee sale numbers
        ts_pattern = re.compile(r"T\.S\.?\s*#[:\s]*([A-Z0-9\-]+)", re.IGNORECASE)

        # Extract loan amounts
        loan_pattern = re.compile(r"(?:principal|original.*amount|loan amount)[:\s]*\$?([\d,]+(?:\.\d{2})?)", re.IGNORECASE)

        # Extract grantor/borrower
        grantor_pattern = re.compile(r"(?:grantor|executed by|trustor)[:\s(]*([A-Z][A-Z\s,]+?)(?:\)|,|\n|and)", re.IGNORECASE)

        # Find all addresses
        addresses = simple_addr.findall(text)
        ts_numbers = ts_pattern.findall(text)
        loan_amounts = loan_pattern.findall(text)
        grantors = grantor_pattern.findall(text)

        # Get auction date - first Tuesday of next month
        auction_date = self._next_first_tuesday()

        for i, addr in enumerate(addresses):
            addr = addr.strip()
            if len(addr) < 10:
                continue

            ts_num = ts_numbers[i] if i < len(ts_numbers) else f"DALLAS-{i}"
            loan = loan_amounts[i] if i < len(loan_amounts) else None
            grantor = grantors[i].strip() if i < len(grantors) else None

            records.append({
                "_source_url": source_url,
                "document_number": ts_num,
                "property_address": addr,
                "city": city,
                "state": self.STATE,
                "county": self.county,
                "borrower_name": grantor,
                "loan_amount": self.parse_money(loan) if loan else None,
                "auction_date": auction_date,
                "notice_type": "NTS",
            })

        return records

    def _next_first_tuesday(self) -> str:
        today = date.today()
        if today.month == 12:
            next_month = date(today.year + 1, 1, 1)
        else:
            next_month = date(today.year, today.month + 1, 1)
        day = next_month
        while day.weekday() != 1:
            day += timedelta(days=1)
        return day.isoformat()

    def get_doc_key(self, raw: dict) -> str:
        return raw.get("document_number", "")

    def get_source_url(self, raw: dict) -> str:
        return raw.get("_source_url", self.FORECLOSURE_PAGE)

    def parse_record(self, raw: dict) -> Optional[dict]:
        doc_num = raw.get("document_number")
        if not doc_num:
            return None
        return {
            "county": self.county,
            "state": self.STATE,
            "document_number": doc_num,
            "notice_type": raw.get("notice_type", "NTS"),
            "auction_date": raw.get("auction_date"),
            "borrower_name": raw.get("borrower_name"),
            "property_address": raw.get("property_address"),
            "loan_amount": raw.get("loan_amount"),
            "raw_data": raw,
        }

    def store_record(self, parsed: dict) -> str:
        property_id = find_or_create_property(
            county=self.county,
            apn=None,
            address_raw=parsed.get("property_address"),
            owner_name=parsed.get("borrower_name"),
            source_id=parsed.get("source_id"),
            source_url=parsed.get("source_url"),
            extra_fields={"state": self.STATE},
        )

        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO foreclosure_events
                  (county, document_number, property_id, source_id, source_doc_id,
                   source_url, notice_type, auction_date, borrower_name,
                   property_address, loan_amount, raw_data)
                VALUES
                  (%(county)s, %(document_number)s, %(property_id)s, %(source_id)s,
                   %(source_doc_id)s, %(source_url)s, %(notice_type)s, %(auction_date)s,
                   %(borrower_name)s, %(property_address)s, %(loan_amount)s,
                   %(raw_data)s::jsonb)
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
