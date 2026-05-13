"""
Maricopa County scrapers.

Scrapes publicly available data from:
- Maricopa County Recorder (foreclosure/trustee documents)
- Maricopa County Assessor (property data, owner info)
- Maricopa County Treasurer (tax delinquency)
- Maricopa County Code Compliance (violations)

All scrapers:
  - Only access public-facing search interfaces
  - Respect rate limits (≤ 1 req/sec unless configured lower)
  - Do NOT bypass login walls or access controls
  - Record source URLs for every record
  - Deduplicate by document number / APN
"""
import re
import csv
import json
import logging
import time
from datetime import date
from io import StringIO
from typing import Optional

import requests
from bs4 import BeautifulSoup

from core.base_scraper import BaseScraper
from core.enrichment import find_or_create_property, enrich_property_from_assessor
from core.address_utils import normalize_apn, normalize_address
from core.config import get_cursor, execute_upsert

log = logging.getLogger("scraper.maricopa")


# ──────────────────────────────────────────────────────────────
# Maricopa Recorder – Foreclosure / Trustee Sale Notices
# ──────────────────────────────────────────────────────────────

class MaricopaRecorderScraper(BaseScraper):
    """
    Queries the Maricopa County Recorder public document search
    for Notice of Trustee Sale (NTS) and Notice of Default (NOD)
    filings. Returns only public document index data.

    Source: https://recorder.maricopa.gov/recdocdata/
    """

    source_key = "maricopa_recorder_foreclosure"
    county = "Maricopa"
    doc_type = "foreclosure"

    # Document type codes in Maricopa recorder system (public)
    NOTICE_TYPES = {
        "NTS": "NOTICE OF TRUSTEE SALE",
        "NOD": "NOTICE OF DEFAULT",
        "NOS": "NOTICE OF SALE",
    }

    BASE_SEARCH = "https://recorder.maricopa.gov/recdocdata/GetDocuments.aspx"

    def fetch_records(self) -> list[dict]:
        """
        Fetch recent foreclosure-related documents from the public recorder index.
        Uses the open document search API (no login required).
        """
        records = []
        # Search last 90 days for each notice type
        from datetime import datetime, timedelta
        end = datetime.today()
        start = end - timedelta(days=90)
        date_from = start.strftime("%m/%d/%Y")
        date_to = end.strftime("%m/%d/%Y")

        for doc_code in ["NTS", "NOD"]:
            try:
                params = {
                    "DocType": doc_code,
                    "DateFrom": date_from,
                    "DateTo": date_to,
                    "RecordedCounty": "03",  # Maricopa FIPS
                }
                resp = self.get(self.BASE_SEARCH, params=params)
                if resp.status_code == 200:
                    parsed = self._parse_recorder_response(resp.text, doc_code)
                    for r in parsed:
                        r["_source_url"] = resp.url
                    records.extend(parsed)
                    log.info("Recorder %s: found %d records", doc_code, len(parsed))
                else:
                    log.warning("Recorder returned HTTP %d for %s", resp.status_code, doc_code)
                time.sleep(2)  # polite delay
            except Exception as e:
                log.error("Error fetching recorder %s: %s", doc_code, e)

        return records

    def _parse_recorder_response(self, html: str, notice_type: str) -> list[dict]:
        """Parse HTML table from recorder search results."""
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        table = soup.find("table", {"id": re.compile(r"grid|result|document", re.I)})
        if not table:
            table = soup.find("table")
        if not table:
            return []

        headers = [th.get_text(strip=True).lower().replace(" ", "_")
                   for th in table.find_all("th")]

        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            row = dict(zip(headers, cells))
            row["_notice_type"] = notice_type
            rows.append(row)

        return rows

    def get_doc_key(self, raw: dict) -> str:
        return raw.get("document_number", raw.get("doc_number", raw.get("instrument", "")))

    def get_source_url(self, raw: dict) -> str:
        doc_num = self.get_doc_key(raw)
        return f"https://recorder.maricopa.gov/recdocdata/GetDocumentDetail.aspx?DocNum={doc_num}"

    def parse_record(self, raw: dict) -> Optional[dict]:
        doc_num = self.get_doc_key(raw)
        if not doc_num:
            return None

        address_raw = raw.get("property_address", raw.get("address", ""))
        apn_raw = raw.get("apn", raw.get("parcel", ""))

        return {
            "county": self.county,
            "document_number": doc_num,
            "notice_type": raw.get("_notice_type", "NTS"),
            "recording_date": self.parse_date(raw.get("recording_date", raw.get("recorded_date"))),
            "trustee_name": self.clean_text(raw.get("trustee")),
            "beneficiary_name": self.clean_text(raw.get("beneficiary", raw.get("lender"))),
            "borrower_name": self.clean_text(raw.get("grantor", raw.get("borrower"))),
            "property_address": self.clean_text(address_raw),
            "apn": normalize_apn(apn_raw, self.county),
            "loan_amount": self.parse_money(raw.get("amount", raw.get("loan_amount"))),
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
            source_id=parsed.get("source_id"),
            source_url=parsed.get("source_url"),
        )

        row = {
            "county": county,
            "document_number": parsed["document_number"],
            "property_id": property_id,
            "source_id": parsed["source_id"],
            "source_doc_id": parsed.get("source_doc_id"),
            "source_url": parsed.get("source_url"),
            "apn": apn,
            "notice_type": parsed.get("notice_type"),
            "recording_date": parsed.get("recording_date"),
            "trustee_name": parsed.get("trustee_name"),
            "beneficiary_name": parsed.get("beneficiary_name"),
            "borrower_name": parsed.get("borrower_name"),
            "property_address": address,
            "loan_amount": parsed.get("loan_amount"),
            "raw_data": json.dumps(parsed.get("raw_data", {})),
        }

        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO foreclosure_events
                  (county, document_number, property_id, source_id, source_doc_id,
                   source_url, apn, notice_type, recording_date, trustee_name,
                   beneficiary_name, borrower_name, property_address,
                   loan_amount, raw_data)
                VALUES
                  (%(county)s, %(document_number)s, %(property_id)s, %(source_id)s,
                   %(source_doc_id)s, %(source_url)s, %(apn)s, %(notice_type)s,
                   %(recording_date)s, %(trustee_name)s, %(beneficiary_name)s,
                   %(borrower_name)s, %(property_address)s, %(loan_amount)s,
                   %(raw_data)s::jsonb)
                ON CONFLICT (county, document_number)
                DO UPDATE SET
                   property_id = EXCLUDED.property_id,
                   notice_type = EXCLUDED.notice_type,
                   trustee_name = EXCLUDED.trustee_name,
                   loan_amount = EXCLUDED.loan_amount,
                   updated_at = NOW()
                """,
                row,
            )
            return "new"


# ──────────────────────────────────────────────────────────────
# Maricopa Assessor – CSV / Open Data
# ──────────────────────────────────────────────────────────────

class MaricopaAssessorCSVScraper(BaseScraper):
    """
    Downloads Maricopa County Assessor open data CSV exports.
    These are published annually and contain parcel-level data
    including owner info, valuations, and property characteristics.

    Source: https://www.maricopa.gov/OpenData (publicly available)
    """

    source_key = "maricopa_assessor_csv"
    county = "Maricopa"
    doc_type = "assessor"

    # Maricopa open data portal - publicly available CSV
    CSV_URLS = [
        "https://www.maricopa.gov/DocumentCenter/View/65243/",  # Residential parcels
        "https://www.maricopa.gov/DocumentCenter/View/65244/",  # Commercial parcels
    ]

    def fetch_records(self) -> list[dict]:
        records = []
        for url in self.CSV_URLS:
            try:
                resp = self.get(url)
                if resp.status_code == 200 and "text/csv" in resp.headers.get("content-type", ""):
                    reader = csv.DictReader(StringIO(resp.text))
                    for row in reader:
                        row["_source_url"] = url
                        records.append(row)
                    log.info("Assessor CSV: loaded %d rows from %s", len(records), url)
                else:
                    log.warning("Assessor CSV not available at %s (HTTP %d)", url, resp.status_code)
            except Exception as e:
                log.error("Error fetching assessor CSV %s: %s", url, e)
        return records

    def get_doc_key(self, raw: dict) -> str:
        return raw.get("APN", raw.get("PARCEL_NUM", raw.get("apn", "")))

    def get_source_url(self, raw: dict) -> str:
        return raw.get("_source_url", "")

    def parse_record(self, raw: dict) -> Optional[dict]:
        apn_raw = self.get_doc_key(raw)
        if not apn_raw:
            return None

        apn = normalize_apn(apn_raw, self.county)
        addr_raw = " ".join(filter(None, [
            raw.get("SITUS_NUM"), raw.get("SITUS_DIR"), raw.get("SITUS_STREET"),
            raw.get("SITUS_SUFFIX"), raw.get("SITUS_CITY"), "AZ",
            raw.get("SITUS_ZIP"),
        ]))

        mailing_raw = " ".join(filter(None, [
            raw.get("MAIL_ADDR1"), raw.get("MAIL_ADDR2"),
        ]))

        return {
            "apn": apn,
            "apn_raw": apn_raw,
            "county": self.county,
            "address_raw": addr_raw,
            "owner_name": self.clean_text(raw.get("OWNER_NAME")),
            "owner_mailing_address": self.clean_text(mailing_raw),
            "owner_mailing_city": self.clean_text(raw.get("MAIL_CITY")),
            "owner_mailing_state": self.clean_text(raw.get("MAIL_STATE")),
            "owner_mailing_zip": self.clean_text(raw.get("MAIL_ZIP", "")),
            "assessed_value": self.parse_money(raw.get("ASSESSED_VALUE", raw.get("AV_TOTAL"))),
            "market_value_est": self.parse_money(raw.get("MARKET_VALUE", raw.get("LPV"))),
            "land_value": self.parse_money(raw.get("LAND_VALUE")),
            "improvement_value": self.parse_money(raw.get("IMP_VALUE")),
            "last_sale_price": self.parse_money(raw.get("LAST_SALE_PRICE")),
            "last_sale_date": self.parse_date(raw.get("LAST_SALE_DATE")),
            "property_type": self.clean_text(raw.get("PROPERTY_CLASS", raw.get("USE_CODE"))),
            "sqft": self._parse_int(raw.get("BLDG_SQFT")),
            "lot_sqft": self._parse_int(raw.get("LOT_SQFT")),
            "year_built": self._parse_int(raw.get("YEAR_BUILT")),
            "bedrooms": self._parse_int(raw.get("BEDROOMS")),
            "bathrooms": self.parse_money(raw.get("BATHROOMS")),
        }

    def _parse_int(self, val) -> Optional[int]:
        try:
            return int(float(str(val).replace(",", ""))) if val else None
        except (ValueError, TypeError):
            return None

    def store_record(self, parsed: dict) -> str:
        addr_parts = normalize_address(parsed.get("address_raw", ""))

        property_id = find_or_create_property(
            county=self.county,
            apn=parsed["apn"],
            address_raw=parsed.get("address_raw"),
            owner_name=parsed.get("owner_name"),
            source_id=parsed.get("source_id"),
            source_url=parsed.get("source_url"),
            extra_fields=addr_parts,
        )

        assessor_data = {**parsed, **addr_parts}
        enrich_property_from_assessor(property_id, assessor_data)
        return "updated"


# ──────────────────────────────────────────────────────────────
# Maricopa Treasurer – Tax Delinquency
# ──────────────────────────────────────────────────────────────

class MaricopaTaxScraper(BaseScraper):
    """
    Scrapes Maricopa County Treasurer public tax delinquency data.
    The treasurer publishes a list of delinquent parcels annually
    as a public record requirement under ARS § 42-18053.

    Source: https://mctreasurer.maricopa.gov (public portal)
    """

    source_key = "maricopa_treasurer_tax"
    county = "Maricopa"
    doc_type = "tax_delinquency"

    BASE_URL = "https://mctreasurer.maricopa.gov/TreasurersPortal/TaxSearch"

    def fetch_records(self) -> list[dict]:
        """
        Fetch delinquent tax records. The Maricopa treasurer publishes
        an annual delinquent list as a public record (ARS § 42-18053).
        We use the public search interface.
        """
        records = []
        try:
            # Try to get the published delinquent tax CSV/list
            # This is a public document published per state statute
            resp = self.get(
                "https://mctreasurer.maricopa.gov/TreasurersPortal/DelinquentTaxList",
                params={"format": "csv", "year": date.today().year - 1}
            )
            if resp.status_code == 200 and resp.text.startswith("APN"):
                reader = csv.DictReader(StringIO(resp.text))
                for row in reader:
                    row["_source_url"] = resp.url
                    records.append(row)
                log.info("Tax delinquency: loaded %d records", len(records))
            else:
                log.info("Delinquent tax CSV not directly available; recording source for manual check")
                # Return placeholder to trigger source URL recording
                records = [{
                    "_source_url": self.BASE_URL,
                    "_placeholder": True,
                    "note": "Manual retrieval required - check treasurer portal for published list",
                }]
        except Exception as e:
            log.error("Tax scraper error: %s", e)
        return records

    def get_doc_key(self, raw: dict) -> str:
        return raw.get("APN", raw.get("apn", raw.get("PARCEL", "")))

    def get_source_url(self, raw: dict) -> str:
        apn = self.get_doc_key(raw)
        return f"https://mctreasurer.maricopa.gov/TreasurersPortal/TaxSearch?apn={apn}"

    def parse_record(self, raw: dict) -> Optional[dict]:
        if raw.get("_placeholder"):
            return None
        apn_raw = self.get_doc_key(raw)
        if not apn_raw:
            return None
        return {
            "county": self.county,
            "apn": normalize_apn(apn_raw, self.county),
            "parcel_number": apn_raw,
            "owner_name": self.clean_text(raw.get("OWNER_NAME", raw.get("owner"))),
            "property_address": self.clean_text(raw.get("SITUS_ADDRESS", raw.get("address"))),
            "tax_year": self._parse_year(raw.get("TAX_YEAR", raw.get("year"))),
            "amount_delinquent": self.parse_money(raw.get("AMOUNT_DUE", raw.get("amount_delinquent"))),
            "amount_total_due": self.parse_money(raw.get("TOTAL_DUE")),
            "years_delinquent": self._parse_int(raw.get("YEARS_DELINQUENT")),
            "raw_data": raw,
        }

    def _parse_year(self, val) -> Optional[int]:
        try:
            return int(str(val)[:4]) if val else None
        except (ValueError, TypeError):
            return None

    def _parse_int(self, val) -> Optional[int]:
        try:
            return int(float(str(val))) if val else None
        except (ValueError, TypeError):
            return None

    def store_record(self, parsed: dict) -> str:
        apn = parsed.get("apn")
        property_id = find_or_create_property(
            county=self.county,
            apn=apn,
            address_raw=parsed.get("property_address"),
            source_id=parsed.get("source_id"),
            source_url=parsed.get("source_url"),
        )

        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO tax_distress
                  (county, apn, parcel_number, property_id, source_id, source_doc_id,
                   source_url, owner_name, property_address, tax_year,
                   amount_delinquent, amount_total_due, years_delinquent, raw_data)
                VALUES
                  (%(county)s, %(apn)s, %(parcel_number)s, %(property_id)s,
                   %(source_id)s, %(source_doc_id)s, %(source_url)s,
                   %(owner_name)s, %(property_address)s, %(tax_year)s,
                   %(amount_delinquent)s, %(amount_total_due)s,
                   %(years_delinquent)s, %(raw_data)s::jsonb)
                ON CONFLICT (county, apn, tax_year)
                DO UPDATE SET
                   amount_delinquent = EXCLUDED.amount_delinquent,
                   amount_total_due = EXCLUDED.amount_total_due,
                   property_id = EXCLUDED.property_id,
                   updated_at = NOW()
                """,
                {**parsed, "property_id": property_id,
                 "raw_data": json.dumps(parsed.get("raw_data", {}))},
            )
        return "new"


# ──────────────────────────────────────────────────────────────
# Maricopa Code Violations
# ──────────────────────────────────────────────────────────────

class MaricopaCodeViolationScraper(BaseScraper):
    """
    Retrieves Maricopa County code compliance public case data.
    Only accesses publicly listed open violation cases.

    Source: https://www.maricopa.gov/1946/Code-Compliance
    """

    source_key = "maricopa_code_violations"
    county = "Maricopa"
    doc_type = "code_violation"

    # Public open data endpoint for code violations
    OPEN_DATA_URL = "https://data.maricopa.gov/api/explore/v2.1/catalog/datasets/code-violations/exports/json"

    def fetch_records(self) -> list[dict]:
        records = []
        try:
            # Attempt public open data API (Socrata/ArcGIS pattern)
            resp = self.get(self.OPEN_DATA_URL, params={"limit": 5000})
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    for row in data:
                        row["_source_url"] = self.OPEN_DATA_URL
                    records = data
                    log.info("Code violations: loaded %d records", len(records))
                else:
                    log.warning("Unexpected response format from code violations API")
            else:
                log.info("Code violations open data not available (HTTP %d)", resp.status_code)
        except Exception as e:
            log.error("Code violation scraper error: %s", e)
        return records

    def get_doc_key(self, raw: dict) -> str:
        return str(raw.get("case_number", raw.get("CASE_NUMBER", raw.get("id", ""))))

    def parse_record(self, raw: dict) -> Optional[dict]:
        case_num = self.get_doc_key(raw)
        if not case_num:
            return None

        return {
            "county": self.county,
            "case_number": case_num,
            "violation_type": self.clean_text(raw.get("violation_type", raw.get("VIOLATION_TYPE"))),
            "violation_description": self.clean_text(raw.get("description", raw.get("DESCRIPTION"))),
            "property_address": self.clean_text(raw.get("address", raw.get("PROPERTY_ADDRESS"))),
            "apn": normalize_apn(raw.get("apn", raw.get("APN", "")), self.county),
            "complaint_date": self.parse_date(raw.get("complaint_date", raw.get("DATE_FILED"))),
            "is_open": str(raw.get("status", raw.get("STATUS", "OPEN"))).upper() in ("OPEN", "ACTIVE", "PENDING"),
            "penalty_amount": self.parse_money(raw.get("penalty_amount", raw.get("PENALTY"))),
            "raw_data": raw,
        }

    def store_record(self, parsed: dict) -> str:
        property_id = find_or_create_property(
            county=self.county,
            apn=parsed.get("apn"),
            address_raw=parsed.get("property_address"),
            source_id=parsed.get("source_id"),
            source_url=parsed.get("source_url"),
        )

        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO code_violations
                  (county, case_number, property_id, source_id, source_doc_id,
                   source_url, violation_type, violation_description, property_address,
                   apn, complaint_date, is_open, penalty_amount, raw_data)
                VALUES
                  (%(county)s, %(case_number)s, %(property_id)s, %(source_id)s,
                   %(source_doc_id)s, %(source_url)s, %(violation_type)s,
                   %(violation_description)s, %(property_address)s, %(apn)s,
                   %(complaint_date)s, %(is_open)s, %(penalty_amount)s,
                   %(raw_data)s::jsonb)
                ON CONFLICT (county, case_number)
                DO UPDATE SET
                   is_open = EXCLUDED.is_open,
                   property_id = EXCLUDED.property_id,
                   updated_at = NOW()
                """,
                {**parsed, "property_id": property_id,
                 "raw_data": json.dumps(parsed.get("raw_data", {}))},
            )
        return "new"
