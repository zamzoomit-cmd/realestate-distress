"""
Pima County scrapers.

Scrapes publicly available data from:
- Pima County Recorder (foreclosure/trustee documents)
- Pima County Assessor (property data - open data portal)
- Pima County Treasurer (tax delinquency)
- Pima County Development Services (code violations)
- Pima Superior Court (probate cases)

All scrapers only access publicly available data and do NOT
bypass any authentication or access controls.
"""
import csv
import json
import re
import logging
import time
from io import StringIO
from typing import Optional

from bs4 import BeautifulSoup

from core.base_scraper import BaseScraper
from core.enrichment import find_or_create_property, enrich_property_from_assessor
from core.address_utils import normalize_apn, normalize_address
from core.config import get_cursor

log = logging.getLogger("scraper.pima")


# ──────────────────────────────────────────────────────────────
# Pima Recorder – Foreclosure Documents
# ──────────────────────────────────────────────────────────────

class PimaRecorderScraper(BaseScraper):
    """
    Queries the Pima County Recorder's public document search for
    Notice of Trustee Sale and Notice of Default filings.

    Source: https://recorder.pima.gov/RecorderWeb/ (public access)
    """

    source_key = "pima_recorder_foreclosure"
    county = "Pima"
    doc_type = "foreclosure"

    SEARCH_URL = "https://recorder.pima.gov/RecorderWeb/search/SearchDocuments"

    def fetch_records(self) -> list[dict]:
        from datetime import datetime, timedelta
        records = []
        end = datetime.today()
        start = end - timedelta(days=90)

        for doc_type in ["NTS", "NOD", "NOTICE OF TRUSTEE SALE"]:
            try:
                payload = {
                    "documentType": doc_type,
                    "startDate": start.strftime("%Y-%m-%d"),
                    "endDate": end.strftime("%Y-%m-%d"),
                    "pageSize": 200,
                    "pageNumber": 1,
                }
                resp = self.post(self.SEARCH_URL, data=payload)
                if resp.status_code == 200:
                    parsed = self._parse_results(resp.text, resp.url, doc_type)
                    records.extend(parsed)
                    log.info("Pima recorder %s: %d records", doc_type, len(parsed))
                else:
                    log.warning("Pima recorder HTTP %d for %s", resp.status_code, doc_type)
                time.sleep(2)
            except Exception as e:
                log.error("Pima recorder error for %s: %s", doc_type, e)

        return records

    def _parse_results(self, html: str, source_url: str, notice_type: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        table = soup.find("table")
        if not table:
            return []

        headers = [th.get_text(strip=True).lower().replace(" ", "_")
                   for th in table.find_all("th")]
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2:
                continue
            row = dict(zip(headers, cells))
            row["_notice_type"] = notice_type
            row["_source_url"] = source_url
            rows.append(row)
        return rows

    def get_doc_key(self, raw: dict) -> str:
        return raw.get("document_number", raw.get("recording_number", raw.get("seq_no", "")))

    def get_source_url(self, raw: dict) -> str:
        doc = self.get_doc_key(raw)
        return f"https://recorder.pima.gov/RecorderWeb/document/{doc}"

    def parse_record(self, raw: dict) -> Optional[dict]:
        doc_num = self.get_doc_key(raw)
        if not doc_num:
            return None
        return {
            "county": self.county,
            "document_number": doc_num,
            "notice_type": raw.get("_notice_type", "NTS"),
            "recording_date": self.parse_date(raw.get("recording_date", raw.get("recorded"))),
            "trustee_name": self.clean_text(raw.get("trustee", raw.get("grantee"))),
            "beneficiary_name": self.clean_text(raw.get("lender", raw.get("beneficiary"))),
            "borrower_name": self.clean_text(raw.get("grantor", raw.get("borrower"))),
            "property_address": self.clean_text(raw.get("property_address", raw.get("address"))),
            "apn": normalize_apn(raw.get("apn", raw.get("parcel", "")), self.county),
            "loan_amount": self.parse_money(raw.get("amount")),
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
                   notice_type = EXCLUDED.notice_type,
                   property_id = EXCLUDED.property_id,
                   updated_at = NOW()
                """,
                {**parsed, "property_id": property_id,
                 "raw_data": json.dumps(parsed.get("raw_data", {}))},
            )
        return "new"


# ──────────────────────────────────────────────────────────────
# Pima Assessor – Open Data (GIS / ArcGIS REST)
# ──────────────────────────────────────────────────────────────

class PimaAssessorScraper(BaseScraper):
    """
    Downloads parcel data from Pima County's public GIS open data portal.
    Uses the ArcGIS REST API which provides public parcel data including
    ownership, valuations, and property characteristics.

    Source: https://gisdata.pima.gov (public open data)
    """

    source_key = "pima_assessor"
    county = "Pima"
    doc_type = "assessor"

    # Pima County GIS public REST service
    ARCGIS_URL = (
        "https://gisdata.pima.gov/arcgis/rest/services/Parcel/PimaParcelData/MapServer/0/query"
    )

    def fetch_records(self) -> list[dict]:
        records = []
        offset = 0
        batch = 500

        while True:
            try:
                params = {
                    "where": "1=1",
                    "outFields": "*",
                    "returnGeometry": "false",
                    "f": "json",
                    "resultOffset": offset,
                    "resultRecordCount": batch,
                }
                resp = self.get(self.ARCGIS_URL, params=params)
                if resp.status_code != 200:
                    log.warning("Pima assessor HTTP %d at offset %d", resp.status_code, offset)
                    break

                data = resp.json()
                features = data.get("features", [])
                if not features:
                    break

                for f in features:
                    attrs = f.get("attributes", {})
                    attrs["_source_url"] = self.ARCGIS_URL
                    records.append(attrs)

                offset += batch
                if len(features) < batch:
                    break

                time.sleep(1)  # polite pacing

            except Exception as e:
                log.error("Pima assessor fetch error at offset %d: %s", offset, e)
                break

        log.info("Pima assessor: loaded %d parcel records", len(records))
        return records

    def get_doc_key(self, raw: dict) -> str:
        return str(raw.get("APN", raw.get("PARCEL_SEQ_NO", raw.get("OBJECTID", ""))))

    def parse_record(self, raw: dict) -> Optional[dict]:
        apn_raw = raw.get("APN", raw.get("PARCEL_SEQ_NO", ""))
        if not apn_raw:
            return None

        addr_raw = " ".join(filter(None, [
            str(raw.get("SITUS_STNO", "")),
            raw.get("SITUS_DIR", ""),
            raw.get("SITUS_STNAME", ""),
            raw.get("SITUS_STTYP", ""),
            raw.get("SITUS_CITY", ""),
            "AZ",
            str(raw.get("SITUS_ZIP", "")),
        ]))

        return {
            "apn": normalize_apn(str(apn_raw), self.county),
            "apn_raw": str(apn_raw),
            "county": self.county,
            "address_raw": addr_raw,
            "owner_name": self.clean_text(raw.get("OWNER_NAME1")),
            "owner_mailing_address": self.clean_text(raw.get("MAIL_ADDR1")),
            "owner_mailing_city": self.clean_text(raw.get("MAIL_CITY")),
            "owner_mailing_state": self.clean_text(raw.get("MAIL_STATE")),
            "owner_mailing_zip": self.clean_text(str(raw.get("MAIL_ZIP", ""))),
            "assessed_value": self.parse_money(raw.get("AV_TOTAL")),
            "market_value_est": self.parse_money(raw.get("FULL_CASH_VALUE")),
            "land_value": self.parse_money(raw.get("AV_LAND")),
            "improvement_value": self.parse_money(raw.get("AV_IMP")),
            "last_sale_price": self.parse_money(raw.get("LAST_SALE_AMT")),
            "last_sale_date": self.parse_date(raw.get("LAST_SALE_DATE")),
            "property_type": self.clean_text(raw.get("PROP_TYPE_DESCR")),
            "sqft": self._to_int(raw.get("GLA_SQFT")),
            "lot_sqft": self._to_int(raw.get("LOT_SIZE_SQFT")),
            "year_built": self._to_int(raw.get("YEAR_BUILT")),
            "bedrooms": self._to_int(raw.get("BEDROOMS")),
            "bathrooms": self.parse_money(raw.get("BATHROOMS")),
            "zoning": self.clean_text(raw.get("ZONING")),
        }

    def _to_int(self, val) -> Optional[int]:
        try:
            return int(float(str(val))) if val else None
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
# Pima Treasurer – Tax Delinquency
# ──────────────────────────────────────────────────────────────

class PimaTaxScraper(BaseScraper):
    """
    Retrieves Pima County tax delinquency data.
    The treasurer is required by ARS § 42-18053 to publish
    the delinquent tax list.

    Source: https://www.to.pima.gov (public portal)
    """

    source_key = "pima_treasurer_tax"
    county = "Pima"
    doc_type = "tax_delinquency"

    BASE_URL = "https://www.to.pima.gov"
    DELINQUENT_URL = "https://www.to.pima.gov/delinquent-tax-list"

    def fetch_records(self) -> list[dict]:
        records = []
        try:
            resp = self.get(self.DELINQUENT_URL)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")

                # Look for CSV download link
                csv_link = soup.find("a", href=re.compile(r"\.csv", re.I))
                if csv_link:
                    csv_url = csv_link["href"]
                    if not csv_url.startswith("http"):
                        csv_url = self.BASE_URL + csv_url
                    csv_resp = self.get(csv_url)
                    if csv_resp.status_code == 200:
                        reader = csv.DictReader(StringIO(csv_resp.text))
                        for row in reader:
                            row["_source_url"] = csv_url
                            records.append(row)
                        log.info("Pima tax: loaded %d delinquent records", len(records))
                else:
                    # Parse HTML table directly
                    table = soup.find("table")
                    if table:
                        headers = [th.get_text(strip=True).lower().replace(" ", "_")
                                   for th in table.find_all("th")]
                        for tr in table.find_all("tr")[1:]:
                            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                            if cells:
                                row = dict(zip(headers, cells))
                                row["_source_url"] = self.DELINQUENT_URL
                                records.append(row)
                        log.info("Pima tax HTML table: %d records", len(records))
            else:
                log.warning("Pima treasurer HTTP %d", resp.status_code)
        except Exception as e:
            log.error("Pima tax scraper error: %s", e)
        return records

    def get_doc_key(self, raw: dict) -> str:
        return raw.get("APN", raw.get("apn", raw.get("parcel_number", "")))

    def parse_record(self, raw: dict) -> Optional[dict]:
        apn_raw = self.get_doc_key(raw)
        if not apn_raw:
            return None
        year = date.today().year - 1
        return {
            "county": self.county,
            "apn": normalize_apn(apn_raw, self.county),
            "parcel_number": apn_raw,
            "owner_name": self.clean_text(raw.get("owner_name", raw.get("OWNER_NAME"))),
            "property_address": self.clean_text(raw.get("address", raw.get("PROPERTY_ADDRESS"))),
            "tax_year": self._to_int(raw.get("tax_year", raw.get("TAX_YEAR", year))),
            "amount_delinquent": self.parse_money(raw.get("amount_due", raw.get("AMOUNT_DUE"))),
            "amount_total_due": self.parse_money(raw.get("total_due", raw.get("TOTAL_DUE"))),
            "raw_data": raw,
        }

    def _to_int(self, val) -> Optional[int]:
        try:
            return int(float(str(val)[:4])) if val else date.today().year - 1
        except (ValueError, TypeError):
            return date.today().year - 1

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
                INSERT INTO tax_distress
                  (county, apn, parcel_number, property_id, source_id, source_doc_id,
                   source_url, owner_name, property_address, tax_year,
                   amount_delinquent, amount_total_due, raw_data)
                VALUES
                  (%(county)s, %(apn)s, %(parcel_number)s, %(property_id)s,
                   %(source_id)s, %(source_doc_id)s, %(source_url)s,
                   %(owner_name)s, %(property_address)s, %(tax_year)s,
                   %(amount_delinquent)s, %(amount_total_due)s, %(raw_data)s::jsonb)
                ON CONFLICT (county, apn, tax_year)
                DO UPDATE SET
                   amount_delinquent = EXCLUDED.amount_delinquent,
                   property_id = EXCLUDED.property_id,
                   updated_at = NOW()
                """,
                {**parsed, "property_id": property_id,
                 "raw_data": json.dumps(parsed.get("raw_data", {}))},
            )
        return "new"


# ──────────────────────────────────────────────────────────────
# Pima Probate Cases
# ──────────────────────────────────────────────────────────────

class PimaProbateScraper(BaseScraper):
    """
    Scrapes Pima County Superior Court public probate case index.
    Court records are public under Arizona Rules of Court.

    Source: https://www.sc.pima.gov (public court records)
    """

    source_key = "pima_superior_court_probate"
    county = "Pima"
    doc_type = "probate"

    SEARCH_URL = "https://www.sc.pima.gov/CourtDocs/Search"

    def fetch_records(self) -> list[dict]:
        records = []
        try:
            # Search for probate case types (PB = Probate in AZ)
            from datetime import datetime, timedelta
            start = (datetime.today() - timedelta(days=180)).strftime("%m/%d/%Y")
            end = datetime.today().strftime("%m/%d/%Y")

            params = {
                "caseType": "PB",
                "startDate": start,
                "endDate": end,
                "pageSize": 100,
            }
            resp = self.get(self.SEARCH_URL, params=params)
            if resp.status_code == 200:
                parsed = self._parse_case_list(resp.text, resp.url)
                records.extend(parsed)
                log.info("Pima probate: found %d cases", len(parsed))
            else:
                log.warning("Pima probate HTTP %d", resp.status_code)
        except Exception as e:
            log.error("Pima probate scraper error: %s", e)
        return records

    def _parse_case_list(self, html: str, source_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        rows = []
        table = soup.find("table")
        if not table:
            return []
        headers = [th.get_text(strip=True).lower().replace(" ", "_")
                   for th in table.find_all("th")]
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2:
                continue
            row = dict(zip(headers, cells))
            row["_source_url"] = source_url
            rows.append(row)
        return rows

    def get_doc_key(self, raw: dict) -> str:
        return raw.get("case_number", raw.get("case_no", raw.get("docket", "")))

    def get_source_url(self, raw: dict) -> str:
        case = self.get_doc_key(raw)
        return f"https://www.sc.pima.gov/CourtDocs/CaseDetail?caseNumber={case}"

    def parse_record(self, raw: dict) -> Optional[dict]:
        case_num = self.get_doc_key(raw)
        if not case_num:
            return None
        return {
            "county": self.county,
            "case_number": case_num,
            "case_type": self.clean_text(raw.get("case_type", "PB")),
            "decedent_name": self.clean_text(raw.get("decedent", raw.get("party_name", raw.get("name")))),
            "personal_rep_name": self.clean_text(raw.get("personal_rep", raw.get("executor"))),
            "attorney_name": self.clean_text(raw.get("attorney")),
            "filing_date": self.parse_date(raw.get("filing_date", raw.get("filed_date"))),
            "case_status": self.clean_text(raw.get("status", "OPEN")),
            "raw_data": raw,
        }

    def store_record(self, parsed: dict) -> str:
        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO probate_cases
                  (county, case_number, source_id, source_doc_id, source_url,
                   case_type, decedent_name, personal_rep_name, attorney_name,
                   filing_date, case_status, raw_data)
                VALUES
                  (%(county)s, %(case_number)s, %(source_id)s, %(source_doc_id)s,
                   %(source_url)s, %(case_type)s, %(decedent_name)s,
                   %(personal_rep_name)s, %(attorney_name)s, %(filing_date)s,
                   %(case_status)s, %(raw_data)s::jsonb)
                ON CONFLICT (county, case_number)
                DO UPDATE SET
                   case_status = EXCLUDED.case_status,
                   updated_at = NOW()
                """,
                {**parsed,
                 "raw_data": json.dumps(parsed.get("raw_data", {}))},
            )
        return "new"
