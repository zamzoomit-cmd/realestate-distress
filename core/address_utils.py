"""
Address normalization and APN matching utilities.

Normalizes raw address strings into structured components,
and standardizes APN formats across counties for matching.
"""
import re
import logging
from typing import Optional

log = logging.getLogger("core.address")

# ──────────────────────────────────────────────────────────────
# APN Normalization
# ──────────────────────────────────────────────────────────────

# Maricopa: 123-45-678 or 12345678 → 123-45-678
# Pima: 123456789 or 123-45-6789 → normalized
APN_STRIP_RE = re.compile(r"[^0-9]")


def normalize_apn(raw_apn: str, county: str = "") -> Optional[str]:
    """
    Normalize APN to a consistent format for a given county.
    Returns None if the APN looks invalid.
    """
    if not raw_apn:
        return None

    digits = APN_STRIP_RE.sub("", str(raw_apn).strip())

    county_lower = county.lower()

    if "maricopa" in county_lower:
        # Maricopa format: XXX-XX-XXX (9 digits)
        if len(digits) == 9:
            return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
        # Sometimes 8 digits - zero pad section
        if len(digits) == 8:
            return f"{digits[:3]}-{digits[3:5]}-0{digits[5:]}"

    elif "pima" in county_lower:
        # Pima format: XXX-XX-XXXX (10 digits)
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
        if len(digits) == 9:
            return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"

    # Generic fallback — return digits only for matching
    if 7 <= len(digits) <= 12:
        return digits

    log.debug("Could not normalize APN '%s' for county '%s'", raw_apn, county)
    return None


def apns_match(apn1: Optional[str], apn2: Optional[str]) -> bool:
    """Return True if two APNs refer to the same parcel (digits-only comparison)."""
    if not apn1 or not apn2:
        return False
    return APN_STRIP_RE.sub("", apn1) == APN_STRIP_RE.sub("", apn2)


# ──────────────────────────────────────────────────────────────
# Address Normalization
# ──────────────────────────────────────────────────────────────

DIRECTIONAL_MAP = {
    r"\bNORTH\b": "N", r"\bSOUTH\b": "S", r"\bEAST\b": "E", r"\bWEST\b": "W",
    r"\bNORTHEAST\b": "NE", r"\bNORTHWEST\b": "NW",
    r"\bSOUTHEAST\b": "SE", r"\bSOUTHWEST\b": "SW",
}

STREET_SUFFIX_MAP = {
    r"\bSTREET\b": "ST", r"\bAVENUE\b": "AVE", r"\bBOULEVARD\b": "BLVD",
    r"\bDRIVE\b": "DR", r"\bCOURT\b": "CT", r"\bCIRCLE\b": "CIR",
    r"\bLANE\b": "LN", r"\bROAD\b": "RD", r"\bPLACE\b": "PL",
    r"\bWAY\b": "WAY", r"\bTERRACE\b": "TER", r"\bTRAIL\b": "TRL",
    r"\bPARKWAY\b": "PKWY", r"\bHIGHWAY\b": "HWY", r"\bFREEWAY\b": "FWY",
    r"\bLOOP\b": "LOOP", r"\bCROSSING\b": "XING",
}

UNIT_RE = re.compile(
    r"\s+(APT|UNIT|STE|SUITE|#|APARTMENT)\s*([A-Z0-9-]+)\s*$",
    re.IGNORECASE,
)

AZ_CITIES = {
    "PHOENIX", "TUCSON", "MESA", "CHANDLER", "SCOTTSDALE", "GLENDALE",
    "GILBERT", "TEMPE", "PEORIA", "SURPRISE", "GOODYEAR", "BUCKEYE",
    "AVONDALE", "QUEEN CREEK", "CASA GRANDE", "FLAGSTAFF", "YUMA",
    "MARICOPA", "APACHE JUNCTION", "FOUNTAIN HILLS", "PARADISE VALLEY",
    "CAVE CREEK", "CAREFREE", "SUN CITY", "SUN CITY WEST", "ANTHEM",
    "ORO VALLEY", "MARANA", "SAHUARITA", "SOMERTON",
}

AZ_ZIP_RE = re.compile(r"\b8[5-6]\d{3}(?:-\d{4})?\b")
STATE_RE = re.compile(r"\b(AZ|ARIZONA)\b", re.IGNORECASE)


def normalize_address(raw: str) -> dict:
    """
    Parse a raw address string into structured components.

    Returns dict with keys:
        address_full, address_street, address_city, address_zip
    """
    if not raw:
        return {}

    text = raw.upper().strip()

    # Remove extra whitespace
    text = re.sub(r"\s+", " ", text)

    # Extract ZIP
    zip_match = AZ_ZIP_RE.search(text)
    zip_code = zip_match.group(0)[:5] if zip_match else None
    if zip_match:
        text = text[:zip_match.start()] + text[zip_match.end():]

    # Remove state abbreviation
    text = STATE_RE.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)

    # Extract city
    city = None
    for c in sorted(AZ_CITIES, key=len, reverse=True):
        pattern = re.compile(r",?\s*" + re.escape(c) + r"[\s,]*$")
        if pattern.search(text):
            city = c.title()
            text = pattern.sub("", text).strip().rstrip(",").strip()
            break

    # Extract unit
    unit_match = UNIT_RE.search(text)
    unit = None
    if unit_match:
        unit = unit_match.group(0).strip()
        text = text[:unit_match.start()].strip()

    # Normalize directionals and suffixes
    street = text
    for pattern, replacement in DIRECTIONAL_MAP.items():
        street = re.sub(pattern, replacement, street)
    for pattern, replacement in STREET_SUFFIX_MAP.items():
        street = re.sub(pattern, replacement, street)

    street = street.title()
    if unit:
        street = f"{street} {unit.title()}"

    # Reconstruct full address
    parts = [street]
    if city:
        parts.append(city)
    parts.append("AZ")
    if zip_code:
        parts.append(zip_code)

    return {
        "address_full": ", ".join(parts),
        "address_street": street,
        "address_city": city,
        "address_zip": zip_code,
    }


def addresses_match(addr1: Optional[str], addr2: Optional[str],
                    threshold: float = 0.85) -> bool:
    """
    Fuzzy match two address strings using token overlap.
    Returns True if addresses likely refer to the same property.
    """
    if not addr1 or not addr2:
        return False

    def tokens(s: str) -> set:
        # Strip punctuation, split, remove common words
        s = re.sub(r"[^A-Z0-9\s]", " ", s.upper())
        stopwords = {"AZ", "ARIZONA", "ST", "AVE", "RD", "DR", "BLVD", "LN", "CT"}
        return {t for t in s.split() if t not in stopwords and len(t) > 1}

    t1, t2 = tokens(addr1), tokens(addr2)
    if not t1 or not t2:
        return False

    intersection = len(t1 & t2)
    union = len(t1 | t2)
    jaccard = intersection / union if union else 0
    return jaccard >= threshold


# ──────────────────────────────────────────────────────────────
# Owner type detection
# ──────────────────────────────────────────────────────────────

def detect_owner_flags(
    property_city: Optional[str],
    property_state: str = "AZ",
    mailing_state: Optional[str] = None,
    mailing_city: Optional[str] = None,
    mailing_zip: Optional[str] = None,
    property_zip: Optional[str] = None,
) -> dict:
    """
    Detect absentee owner and out-of-state owner flags.
    """
    is_out_of_state = False
    is_absentee = False

    if mailing_state and mailing_state.upper() not in ("AZ", "ARIZONA"):
        is_out_of_state = True
        is_absentee = True
    elif mailing_zip and property_zip and mailing_zip[:5] != property_zip[:5]:
        is_absentee = True
    elif mailing_city and property_city:
        is_absentee = mailing_city.upper() != property_city.upper()

    return {
        "is_absentee_owner": is_absentee,
        "is_out_of_state": is_out_of_state,
    }
