"""May Potenza Trustee Sale Scraper - Fixed HTML parser"""
import json, re, logging
from typing import Optional
from bs4 import BeautifulSoup
from core.base_scraper import BaseScraper
from core.enrichment import find_or_create_property
from core.address_utils import normalize_apn
from core.config import get_cursor

log = logging.getLogger("scraper.maypotenza")

class MayPotenzaTrusteeScraper(BaseScraper):
    source_key = "maypotenza_trustee_sales"
    county = "Maricopa"
    doc_type = "foreclosure"
    URL = "https://www.maypotenza.com/trustee-sales/"

    def fetch_records(self):
        records = []
        try:
            resp = self.get(self.URL)
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Find all property detail sections
            detail_divs = soup.find_all("div", class_="property-details col-lg-8 col-sm-12")
            log.info("Found %d property detail sections", len(detail_divs))
            
            for div in detail_divs:
                try:
                    text = div.get_text(" ", strip=True)
                    
                    # Get parent to find auction date and status
                    wrapper = div.find_parent("div", class_=re.compile("wrapper"))
                    full_text = wrapper.get_text(" ", strip=True) if wrapper else text
                    
                    rec_num = self._find(full_text, r"Recorder'?s #[:\s<>br/]*([0-9]{4}-[0-9]+)")
                    if not rec_num:
                        continue
                    
                    status = self._find(full_text, r"STATUS\s+(\w+)")
                    if status and status.upper() in ("CANCELED", "CANCELLED"):
                        continue

                    records.append({
                        "_source_url": self.URL,
                        "recorder_number": rec_num,
                        "property_address": self._find(text, r"Property Address[:\s]+([^\n]+?)(?:Place of Sale|County|Trustor|Parcel|$)"),
                        "parcel": self._find(text, r"Parcel #[:\s]+([\d\-]+)"),
                        "trustor": self._find(text, r"Trustor[:\s]+([^\n]+?)(?:Parcel|Beneficiary|Opening|$)"),
                        "opening_bid": self._find(text, r"Opening Bid Amount[:\s]+\$?([\d,\.]+)"),
                        "auction_datetime": self._find(full_text, r"(\d{2}/\d{2}/\d{4}\s+\d+:\d+\s+[apm]+)"),
                        "status": status or "SALE",
                        "county_raw": self._find(text, r"County[:\s]+([^\n]+?)(?:Trustor|Parcel|$)"),
                    })
                except Exception as e:
                    log.debug("Parse error: %s", e)

            log.info("Parsed %d active listings", len(records))
        except Exception as e:
            log.error("Fetch error: %s", e, exc_info=True)
        return records

    def _find(self, text, pattern):
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        return re.sub(r"\s+", " ", m.group(1)).strip() or None

    def get_doc_key(self, raw):
        return raw.get("recorder_number", "")

    def get_source_url(self, raw):
        return f"https://recorder.maricopa.gov/recdocdata/GetDocuments.aspx?recno={raw.get('recorder_number','')}"

    def parse_record(self, raw):
        doc_num = raw.get("recorder_number")
        if not doc_num:
            return None
        county_raw = raw.get("county_raw", "")
        county = "Pima" if "pima" in county_raw.lower() else "Maricopa"
        apn = normalize_apn(raw.get("parcel", ""), county)
        try:
            from datetime import datetime
            auction_date = datetime.strptime(raw["auction_datetime"].strip(), "%m/%d/%Y %I:%M %p").date().isoformat()
        except:
            auction_date = self.parse_date(raw.get("auction_datetime"))
        return {
            "county": county,
            "document_number": doc_num,
            "notice_type": "NTS",
            "auction_date": auction_date,
            "trustee_name": "May Potenza Baran & Gillespie",
            "borrower_name": self.clean_text(raw.get("trustor")),
            "property_address": self.clean_text(raw.get("property_address")),
            "apn": apn,
            "opening_bid": self.parse_money(raw.get("opening_bid")),
            "raw_data": raw,
        }

    def store_record(self, parsed):
        property_id = find_or_create_property(
            county=parsed["county"], apn=parsed.get("apn"),
            address_raw=parsed.get("property_address"),
            owner_name=parsed.get("borrower_name"),
cat > scrapers/maricopa/maypotenza_scraper.py << 'ENDOFFILE'
"""May Potenza Trustee Sale Scraper - Fixed HTML parser"""
import json, re, logging
from typing import Optional
from bs4 import BeautifulSoup
from core.base_scraper import BaseScraper
from core.enrichment import find_or_create_property
from core.address_utils import normalize_apn
from core.config import get_cursor

log = logging.getLogger("scraper.maypotenza")

class MayPotenzaTrusteeScraper(BaseScraper):
    source_key = "maypotenza_trustee_sales"
    county = "Maricopa"
    doc_type = "foreclosure"
    URL = "https://www.maypotenza.com/trustee-sales/"

    def fetch_records(self):
        records = []
        try:
            resp = self.get(self.URL)
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Find all property detail sections
            detail_divs = soup.find_all("div", class_="property-details col-lg-8 col-sm-12")
            log.info("Found %d property detail sections", len(detail_divs))
            
            for div in detail_divs:
                try:
                    text = div.get_text(" ", strip=True)
                    
                    # Get parent to find auction date and status
                    wrapper = div.find_parent("div", class_=re.compile("wrapper"))
                    full_text = wrapper.get_text(" ", strip=True) if wrapper else text
                    
                    rec_num = self._find(full_text, r"Recorder'?s #[:\s<>br/]*([0-9]{4}-[0-9]+)")
                    if not rec_num:
                        continue
                    
                    status = self._find(full_text, r"STATUS\s+(\w+)")
                    if status and status.upper() in ("CANCELED", "CANCELLED"):
                        continue

                    records.append({
                        "_source_url": self.URL,
                        "recorder_number": rec_num,
                        "property_address": self._find(text, r"Property Address[:\s]+([^\n]+?)(?:Place of Sale|County|Trustor|Parcel|$)"),
                        "parcel": self._find(text, r"Parcel #[:\s]+([\d\-]+)"),
                        "trustor": self._find(text, r"Trustor[:\s]+([^\n]+?)(?:Parcel|Beneficiary|Opening|$)"),
                        "opening_bid": self._find(text, r"Opening Bid Amount[:\s]+\$?([\d,\.]+)"),
                        "auction_datetime": self._find(full_text, r"(\d{2}/\d{2}/\d{4}\s+\d+:\d+\s+[apm]+)"),
                        "status": status or "SALE",
                        "county_raw": self._find(text, r"County[:\s]+([^\n]+?)(?:Trustor|Parcel|$)"),
                    })
                except Exception as e:
                    log.debug("Parse error: %s", e)

            log.info("Parsed %d active listings", len(records))
        except Exception as e:
            log.error("Fetch error: %s", e, exc_info=True)
        return records

    def _find(self, text, pattern):
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        return re.sub(r"\s+", " ", m.group(1)).strip() or None

    def get_doc_key(self, raw):
        return raw.get("recorder_number", "")

    def get_source_url(self, raw):
        return f"https://recorder.maricopa.gov/recdocdata/GetDocuments.aspx?recno={raw.get('recorder_number','')}"

    def parse_record(self, raw):
        doc_num = raw.get("recorder_number")
        if not doc_num:
            return None
        county_raw = raw.get("county_raw", "")
        county = "Pima" if "pima" in county_raw.lower() else "Maricopa"
        apn = normalize_apn(raw.get("parcel", ""), county)
        try:
            from datetime import datetime
            auction_date = datetime.strptime(raw["auction_datetime"].strip(), "%m/%d/%Y %I:%M %p").date().isoformat()
        except:
            auction_date = self.parse_date(raw.get("auction_datetime"))
        return {
            "county": county,
            "document_number": doc_num,
            "notice_type": "NTS",
            "auction_date": auction_date,
            "trustee_name": "May Potenza Baran & Gillespie",
            "borrower_name": self.clean_text(raw.get("trustor")),
            "property_address": self.clean_text(raw.get("property_address")),
            "apn": apn,
            "opening_bid": self.parse_money(raw.get("opening_bid")),
            "raw_data": raw,
        }

    def store_record(self, parsed):
        property_id = find_or_create_property(
            county=parsed["county"], apn=parsed.get("apn"),
            address_raw=parsed.get("property_address"),
            owner_name=parsed.get("borrower_name"),
            source_id=parsed.get("source_id"),
            source_url=parsed.get("source_url"),
        )
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO foreclosure_events
                  (county, document_number, property_id, source_id, source_doc_id,
                   source_url, apn, notice_type, auction_date, trustee_name,
                   borrower_name, property_address, opening_bid, raw_data)
                VALUES
                  (%(county)s, %(document_number)s, %(property_id)s, %(source_id)s,
                   %(source_doc_id)s, %(source_url)s, %(apn)s, %(notice_type)s,
                   %(auction_date)s, %(trustee_name)s, %(borrower_name)s,
                   %(property_address)s, %(opening_bid)s, %(raw_data)s::jsonb)
                ON CONFLICT (county, document_number) DO UPDATE SET
                   auction_date = EXCLUDED.auction_date,
                   property_id = EXCLUDED.property_id,
                   updated_at = NOW()
            """, {**parsed, "property_id": property_id,
                  "raw_data": json.dumps(parsed.get("raw_data", {}))})
        return "new"
