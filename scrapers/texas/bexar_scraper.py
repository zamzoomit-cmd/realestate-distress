"""Bexar County Texas Foreclosure Scraper - Clean parser"""
import io, json, re, logging
from datetime import date, timedelta
from core.base_scraper import BaseScraper
from core.enrichment import find_or_create_property
from core.config import get_cursor

log = logging.getLogger("scraper.bexar")

def extract_pdf_text(pdf_bytes):
    try:
        from pdfminer.high_level import extract_text
        return extract_text(io.BytesIO(pdf_bytes))
    except Exception as e:
        log.error("PDF error: %s", e)
        return ""

# Valid Bexar County cities
VALID_CITIES = {
    'SAN ANTONIO','ATASCOSA','BOERNE','HELOTES','SOMERSET','VON ORY',
    'CONVERSE','ELMENDORF','ADKINS','UNIVERSAL CITY','LIVE OAK',
    'LEON VALLEY','BALCONES HEIGHTS','KIRBY','WINDCREST','SCHERTZ',
    'SELMA','CIBOLO','MARION','FLOR
cd ~/Downloads/realestate-distress
cat > scrapers/texas/bexar_scraper.py << 'ENDOFFILE'
"""Bexar County Texas Foreclosure Scraper - Clean parser"""
import io, json, re, logging
from datetime import date, timedelta
from core.base_scraper import BaseScraper
from core.enrichment import find_or_create_property
from core.config import get_cursor

log = logging.getLogger("scraper.bexar")

def extract_pdf_text(pdf_bytes):
    try:
        from pdfminer.high_level import extract_text
        return extract_text(io.BytesIO(pdf_bytes))
    except Exception as e:
        log.error("PDF error: %s", e)
        return ""

# Valid Bexar County cities
VALID_CITIES = {
    'SAN ANTONIO','ATASCOSA','BOERNE','HELOTES','SOMERSET','VON ORY',
    'CONVERSE','ELMENDORF','ADKINS','UNIVERSAL CITY','LIVE OAK',
    'LEON VALLEY','BALCONES HEIGHTS','KIRBY','WINDCREST','SCHERTZ',
    'SELMA','CIBOLO','MARION','FLORESVILLE','POTEET','PLEASANTON',
    'LYTLE','NATALIA','CASTROVILLE','LYTLE','HONDO','SAN MARCOS',
    'NEW BRAUNFELS','SEGUIN','LAREDO','LAVERNIA','STOCKDALE',
    'FALLS CITY','POTH','KARNES CITY','KENEDY','CUERO','VICTORIA',
    'CORPUS CHRISTI','GEORGE WEST','BEEVILLE','PLEASANTON',
    'PEARSALL','COTULLA','UVALDE','BRACKETTVILLE','DEL RIO',
    'EAGLE PASS','CRYSTAL CITY','CARRIZO SPRINGS','DILLEY',
    'THREE RIVERS','JOURDANTON','PEARSALL','DIVINE','LYTLE',
    'HILL COUNTRY VILLAGE','OLMOS PARK','TERRELL HILLS','ALAMO HEIGHTS',
    'HOLLYWOOD PARK','SHAVANO PARK','GREY FOREST','FAIR OAKS RANCH',
    'BULVERDE','SPRING BRANCH','CANYON LAKE','GARDEN RIDGE',
    'TIMBERWOOD PARK','LACKLAND','LACKLAND AFB','RANDOLPH','RANDOLPH AFB',
    'KELLY','KELLY AFB','BROOKS','BROOKS CITY','STINSON','SAN ANTOINO',
    'VON ORMY','MACDONA','GREY FOREST','PIPE CREEK','BANDERA',
    'KERRVILLE','FREDERICKSBURG','JOHNSON CITY','MARBLE FALLS',
    'BURNET','LLANO','MASON','SAN SABA','BRADY','MENARD','JUNCTION',
    'ROCKSPRINGS','UVALDE','SABINAL','HONDO','CASTROVILLE','NATALIA',
    'LYTLE','DEVINE','MOORE','PLEASANTON','KENEDY','KARNES CITY',
    'CUERO','YOAKUM','SHINER','GONZALES','SEGUIN','NEW BRAUNFELS',
    'SAN MARCOS','KYLE','BUDA','AUSTIN','ROUND ROCK','CEDAR PARK',
}

class BexarForeclosureScraper(BaseScraper):
    source_key = "bexar_foreclosure"
    county = "Bexar"
    doc_type = "foreclosure"
    STATE = "TX"
    PDF_URL = "https://www.bexar.org/DocumentCenter/View/505/Current-County-Clerk-Foreclosures"

    def fetch_records(self):
        records = []
        try:
            resp = self.get(self.PDF_URL)
            if resp.status_code == 200:
                text = extract_pdf_text(resp.content)
                log.info("Bexar PDF text: %d chars", len(text))
                records = self._parse_text(text)
                log.info("Bexar: found %d valid records", len(records))
        except Exception as e:
            log.error("Bexar error: %s", e, exc_info=True)
        return records

    def _parse_text(self, text):
        records = []
        doc_nums = re.findall(r'\b(\d{4}[A-Z]{0,2}\d{6,})\b', text)
        type_addr = re.findall(r'(MORTGAGE|TAX|HELOC|HOA)\s+([A-Z0-9][^\n]+?)(?=\n)', text, re.IGNORECASE)
        city_match = re.search(r'CITY/TOWN\s*\n(.*)', text, re.DOTALL)
        cities = []
        if city_match:
            for line in city_match.group(1).split('\n'):
                line = line.strip().upper()
                if line and not re.match(r'^\d', line) and len(line) > 2:
                    cities.append(line)
        auction_date = self._next_first_tuesday()
        log.info("Bexar: %d doc nums, %d addresses, %d cities", len(doc_nums), len(type_addr), len(cities))
        for i, (notice_type, address) in enumerate(type_addr):
            address = address.strip()
            # Skip bad addresses
            if any(x in address.upper() for x in ['MORTGAGE', 'FORECLOSURE', 'PAGE', 'CITY/TOWN', 'DOCUMENT', 'NUMBER', 'TYPE', 'ADDRESS', 'LUCY', 'COUNTY CLERK']):
                continue
            doc_num = doc_nums[i] if i < len(doc_nums) else f"BEXAR-{i}"
            # Get city and validate it
            city = cities[i] if i < len(cities) else "SAN ANTONIO"
            # If city looks invalid, default to SAN ANTONIO
            if any(x in city for x in ['MORTGAGE', 'PAGE', 'FORECLOSURE', 'DOCUMENT', 'NUMBER', 'TYPE', 'ADDRESS', 'LUCY', 'COUNTY']):
                city = "SAN ANTONIO"
            records.append({
                "_source_url": self.PDF_URL,
                "document_number": doc_num,
                "notice_type": notice_type.upper(),
                "property_address": f"{address}, {city}, TX",
                "city": city,
                "state": self.STATE,
                "auction_date": auction_date,
            })
        return records

    def _next_first_tuesday(self):
        today = date.today()
        nm = date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
        while nm.weekday() != 1:
            nm += timedelta(days=1)
        return nm.isoformat()

    def get_doc_key(self, raw): return raw.get("document_number", "")
    def get_source_url(self, raw): return self.PDF_URL

    def parse_record(self, raw):
        if not raw.get("document_number"): return None
        return {"county": self.county, "document_number": raw["document_number"],
                "notice_type": raw.get("notice_type", "NTS"), "auction_date": raw.get("auction_date"),
                "property_address": raw.get("property_address"), "raw_data": raw}

    def store_record(self, parsed):
        property_id = find_or_create_property(
            county=self.county, apn=None, address_raw=parsed.get("property_address"),
            source_id=parsed.get("source_id"), source_url=parsed.get("source_url"),
            extra_fields={"state": self.STATE})
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO foreclosure_events
                  (county, document_number, property_id, source_id, source_doc_id,
                   source_url, notice_type, auction_date, property_address, raw_data)
                VALUES (%(county)s, %(document_number)s, %(property_id)s, %(source_id)s,
                   %(source_doc_id)s, %(source_url)s, %(notice_type)s, %(auction_date)s,
                   %(property_address)s, %(raw_data)s::jsonb)
                ON CONFLICT (county, document_number) DO UPDATE SET
                   auction_date = EXCLUDED.auction_date,
                   property_id = EXCLUDED.property_id,
                   updated_at = NOW()
            """, {**parsed, "property_id": property_id,
                  "raw_data": json.dumps(parsed.get("raw_data", {}))})
        return "new"
