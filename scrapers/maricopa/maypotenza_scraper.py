"""
May Potenza Trustee Sale Scraper - Fixed version
"""
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
            text = soup.get_text(" ", strip=True)
            
            # Find all recorder numbers
            rec_numbers = re.findall(r'Recorder\'?s #[:\s]+(\d{4}-\d+)', text)
            addresses = re.findall(r'Property Address[:\s]+(.+?)(?:Place of Sale|County:|Trustor:|Parcel)', text)
            parcels = re.findall(r'Parcel #[:\s]+([\d\-]+)', text)
            trustors = re.findall(r'Trustor[:\s]+(.+?)(?:Parcel|Beneficiary|Opening)', text)
            auction_dates = re.findall(r'(\d{2}/\d{2}/\d{4}\s+\d+:\d+\s+[apm]+)', text)
            opening_bids = re.findall(r'Opening Bid Amount[:\s]+\$?([\d,\.]+)', text)
            statuses = re.findall(r'STATUS\s+(\w+)', text)

            log.info("Found %d recorder numbers on page", len(rec_numbers))
cd ~/Downloads/realestate-distress
cat > scrapers/maricopa/maypotenza_scraper.py << 'ENDOFFILE'
"""
May Potenza Trustee Sale Scraper - Fixed version
"""
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
            text = soup.get_text(" ", strip=True)
            
            # Find all recorder numbers
            rec_numbers = re.findall(r'Recorder\'?s #[:\s]+(\d{4}-\d+)', text)
            addresses = re.findall(r'Property Address[:\s]+(.+?)(?:Place of Sale|County:|Trustor:|Parcel)', text)
            parcels = re.findall(r'Parcel #[:\s]+([\d\-]+)', text)
            trustors = re.findall(r'Trustor[:\s]+(.+?)(?:Parcel|Beneficiary|Opening)', text)
            auction_dates = re.findall(r'(\d{2}/\d{2}/\d{4}\s+\d+:\d+\s+[apm]+)', text)
            opening_bids = re.findall(r'Opening Bid Amount[:\s]+\$?([\d,\.]+)', text)
            statuses = re.findall(r'STATUS\s+(\w+)', text)

            log.info("Found %d recorder numbers on page", len(rec_numbers))

            for i, rec_num in enumerate(rec_numbers):
                status = statuses[i] if i < len(statuses) else "SALE"
                if status.upper() in ("CANCELED", "CANCELLED"):
                    continue
                records.append({
                    "_source_url": self.URL,
                    "recorder_number": rec_num,
                    "property_address": addresses[i].strip() if i < len(addresses) else None,
                    "parcel": parcels[i] if i < len(parcels) else None,
                    "trustor": trustors[i].strip() if i < len(trustors) else None,
                    "auction_datetime": auction_dates[i] if i < len(auction_dates) else None,
                    "opening_bid": opening_bids[i] if i < len(opening_bids) else None,
                    "status": status,
                })
        except Exception as e:
            log.error("Error: %s", e, exc_info=True)
        return records

    def get_doc_key(self, raw):
        return raw.get("recorder_number", "")

    def get_source_url(self, raw):
        return f"https://recorder.maricopa.gov/recdocdata/GetDocuments.aspx?recno={raw.get('recorder_number','')}"

    def parse_record(self, raw):
        doc_num = raw.get("recorder_number")
        if not doc_num:
            return None
        apn = normalize_apn(raw.get("parcel",""), "Maricopa")
        try:
            from datetime import datetime
            auction_date = datetime.strptime(raw.get("auction_datetime","").strip(), "%m/%d/%Y %I:%M %p").date().isoformat()
        except:
            auction_date = self.parse_date(raw.get("auction_datetime"))
        return {
            "county": "Maricopa",
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
            county="Maricopa", apn=parsed.get("apn"),
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
