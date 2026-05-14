"""
May Potenza Baran & Gillespie - Trustee Sale Scraper
Scrapes real active foreclosure listings from maypotenza.com/trustee-sales/

This is a publicly posted list of pending trustee sales in Arizona.
Data includes: property address, APN, auction date, opening bid,
trustor name, county, recorder number.

Source: https://www.maypotenza.com/trustee-sales/
"""
import json
import re
import time
import logging
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

from core.base_scraper import BaseScraper
from core.enrichment import find_or_create_property
from core.address_utils import normalize_apn
from core.config import get_cursor

log = logging.getLogger("scraper.maypotenza")


class MayPotenzaTrusteeScraper(BaseScraper):
    """
    Scrapes the publicly posted trustee sale list from May Potenza law firm.
    This list is published as a public courtesy and contains structured
    foreclosure data for Arizona properties.

    Source: https://www.maypotenza.com/trustee-sales/
    """

    source_key = "maypotenza_trustee_sales"
    county = "Maricopa"
    doc_type = "foreclosure"

    URL = "https://www.maypotenza.com/trustee-sales/"

    def fetch_records(self) -> list[dict]:
        records = []
        try:
            resp = self.get(self.URL)
            if resp.status_code != 200:
                log.warning("May Potenza returned HTTP %d", resp.status_code)
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            records = self._parse_listings(soup, resp.url)
            log.info("May Potenza: found %d trustee sale listings", len(records))

        except Exception as e:
            log.error("May Potenza scraper error: %s", e, exc_info=True)

        return records

    def _parse_listings(self, soup: BeautifulSoup, source_url: str) -> list[dict]:
        records = []

        # Each listing is in a section with property details
        # Look for Recorder's # labels which anchor each listing
        recorder_labels = soup.find_all(string=re.compile(r"Recorder'?s #", re.I))

        for label in recorder_labels:
            try:
                # Walk up to find the container section
                container = label.find_parent()
                for _ in range(10):
                    if container is None:
                        break
                    # Look for the full listing block
                    text = container.get_text(" ", strip=True)
                    if "Property Address" in text and "Trustor" in text:
                        break
                    container = container.find_parent()

                if not container:
                    continue

                text = container.get_text(" ", strip=True)

                rec = {
                    "_source_url": source_url,
                    "recorder_number": self._extract(text, r"Recorder'?s #[:\s]+([0-9\-]+)"),
                    "property_address": self._extract(text, r"Property Address[:\s]+(.+?)(?:Place of Sale|County|Trustor|Parcel|$)"),
                    "county": self._extract(text, r"County[:\s]+(.+?)(?:Trustor|Parcel|Beneficiary|$)"),
                    "trustor": self._extract(text, r"Trustor[:\s]+(.+?)(?:Parcel|Beneficiary|Trustee|Opening|$)"),
                    "parcel": self._extract(text, r"Parcel #[:\s]+([0-9\-]+)"),
                    "beneficiary": self._extract(text, r"Beneficiary[:\s]+(.+?)(?:Trustee|Opening|$)"),
                    "opening_bid": self._extract(text, r"Opening Bid Amount[:\s]+\$?([\d,\.]+)"),
                    "auction_datetime": self._extract(text, r"DATE/TIME OF SALE\s+([0-9/]+ [0-9:]+ [apm]+)"),
                    "status": self._extract(text, r"STATUS\s+(\w+)"),
                    "place_of_sale": self._extract(text, r"Place of Sale[:\s]+(.+?)(?:County|Trustor|$)"),
                }

                # Skip canceled
                if rec.get("status", "").upper() in ("CANCELED", "CANCELLED"):
                    continue

                # Skip if no recorder number
                if not rec.get("recorder_number"):
                    continue

                records.append(rec)

            except Exception as e:
                log.debug("Error parsing listing: %s", e)
                continue

        return records

    def _extract(self, text: str, pattern: str) -> Optional[str]:
        """Extract first match from text, cleaned up."""
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        val = m.group(1).strip()
        # Clean up common artifacts
        val = re.sub(r"\s+", " ", val)
        val = val.rstrip(".,;")
        return val if val else None

    def get_doc_key(self, raw: dict) -> str:
        return raw.get("recorder_number", "")

    def get_source_url(self, raw: dict) -> str:
        rec = raw.get("recorder_number", "")
        return f"https://recorder.maricopa.gov/recdocdata/GetDocuments.aspx?recno={rec}"

    def parse_record(self, raw: dict) -> Optional[dict]:
        doc_num = raw.get("recorder_number")
        if not doc_num:
            return None

        # Determine county
        county_raw = raw.get("county", "")
        if "pima" in county_raw.lower():
            county = "Pima"
        else:
            county = "Maricopa"

        # Parse auction date
        auction_dt = raw.get("auction_datetime")
        auction_date = None
        if auction_dt:
            try:
                auction_date = datetime.strptime(
                    auction_dt.strip(), "%m/%d/%Y %I:%M %p"
                ).date().isoformat()
            except Exception:
                auction_date = self.parse_date(auction_dt)

        # Parse APN
        parcel_raw = raw.get("parcel", "")
        apn = normalize_apn(parcel_raw, county) if parcel_raw else None

        # Parse opening bid
        opening_bid = self.parse_money(raw.get("opening_bid"))

        # Trustor = borrower
        trustor = self.clean_text(raw.get("trustor", ""))

        return {
            "county": county,
            "document_number": doc_num,
            "notice_type": "NTS",
            "recording_date": None,
            "auction_date": auction_date,
            "auction_location": self.clean_text(raw.get("place_of_sale")),
            "trustee_name": "May Potenza Baran & Gillespie",
            "beneficiary_name": self.clean_text(raw.get("beneficiary")),
            "borrower_name": trustor,
            "property_address": self.clean_text(raw.get("property_address")),
            "apn": apn,
            "opening_bid": opening_bid,
            "raw_data": raw,
        }

    def store_record(self, parsed: dict) -> str:
        county = parsed["county"]
        apn = parsed.get("apn")
        address = parsed.get("property_address")

        property_id = find_or_create_property(
            county=county,
            apn=apn,
            address_raw=address,
            owner_name=parsed.get("borrower_name"),
            source_id=parsed.get("source_id"),
            source_url=parsed.get("source_url"),
        )

        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO foreclosure_events
                  (county, document_number, property_id, source_id, source_doc_id,
                   source_url, apn, notice_type, auction_date, auction_location,
                   trustee_name, beneficiary_name, borrower_name,
                   property_address, opening_bid, raw_data)
                VALUES
                  (%(county)s, %(document_number)s, %(property_id)s, %(source_id)s,
                   %(source_doc_id)s, %(source_url)s, %(apn)s, %(notice_type)s,
                   %(auction_date)s, %(auction_location)s, %(trustee_name)s,
                   %(beneficiary_name)s, %(borrower_name)s, %(property_address)s,
                   %(opening_bid)s, %(raw_data)s::jsonb)
                ON CONFLICT (county, document_number)
                DO UPDATE SET
                   auction_date = EXCLUDED.auction_date,
                   opening_bid = EXCLUDED.opening_bid,
                   property_id = EXCLUDED.property_id,
                   updated_at = NOW()
                """,
                {**parsed, "property_id": property_id,
                 "raw_data": json.dumps(parsed.get("raw_data", {}))},
            )
        return "new"
