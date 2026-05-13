"""
Property enrichment - links distress records to canonical property
records, runs APN matching, enriches equity estimates, and detects
owner flags.
"""
import uuid
import logging
from typing import Optional

from core.config import get_cursor, settings
from core.address_utils import (
    normalize_apn, normalize_address, apns_match,
    addresses_match, detect_owner_flags
)
from core.scoring import estimate_equity, compute_equity_tier

log = logging.getLogger("core.enrichment")


# ──────────────────────────────────────────────────────────────
# Property lookup / create
# ──────────────────────────────────────────────────────────────

def find_or_create_property(
    county: str,
    apn: Optional[str] = None,
    address_raw: Optional[str] = None,
    owner_name: Optional[str] = None,
    source_id: Optional[str] = None,
    source_url: Optional[str] = None,
    extra_fields: Optional[dict] = None,
) -> str:
    """
    Finds an existing property by APN or address, or creates a new one.
    Returns property.id (UUID string).
    """
    norm_apn = normalize_apn(apn, county) if apn else None
    addr_parts = normalize_address(address_raw) if address_raw else {}

    property_id = None

    # 1. Try APN match first (most reliable)
    if norm_apn:
        with get_cursor() as cur:
            cur.execute(
                "SELECT id FROM properties WHERE apn = %s AND county = %s LIMIT 1",
                (norm_apn, county),
            )
            row = cur.fetchone()
            if row:
                property_id = str(row["id"])
                log.debug("APN match: %s → %s", norm_apn, property_id)

    # 2. Try address fuzzy match
    if not property_id and addr_parts.get("address_full"):
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, address_full FROM properties
                WHERE county = %s
                  AND address_full % %s
                LIMIT 5
                """,
                (county, addr_parts["address_full"]),
            )
            candidates = cur.fetchall()
            for cand in candidates:
                if addresses_match(addr_parts["address_full"], cand["address_full"]):
                    property_id = str(cand["id"])
                    log.debug("Address match: %s → %s", addr_parts["address_full"], property_id)
                    break

    # 3. Create new property
    if not property_id:
        property_id = str(uuid.uuid4())
        fields = {
            "id": property_id,
            "county": county,
            "state": "AZ",
            "apn": norm_apn,
            "apn_raw": apn,
            "owner_name": owner_name,
            **addr_parts,
        }
        if extra_fields:
            fields.update(extra_fields)
        if source_id:
            fields["source_ids"] = [source_id]
        if source_url:
            fields["source_urls"] = [source_url]

        cols = list(fields.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_str = ", ".join(cols)
        values = [fields[c] for c in cols]

        with get_cursor() as cur:
            cur.execute(
                f"INSERT INTO properties ({col_str}) VALUES ({placeholders}) "
                f"ON CONFLICT DO NOTHING",
                values,
            )
        log.info("Created property %s | APN=%s | Addr=%s", property_id, norm_apn, addr_parts.get("address_full"))

    # Update source tracking
    if property_id and (source_id or source_url):
        _update_property_sources(property_id, source_id, source_url)

    return property_id


def _update_property_sources(property_id: str, source_id: Optional[str], source_url: Optional[str]):
    """Append source_id and source_url to property arrays if not already present."""
    with get_cursor() as cur:
        if source_id:
            cur.execute(
                "UPDATE properties SET source_ids = array_append(source_ids, %s::uuid) "
                "WHERE id = %s AND NOT (source_ids @> ARRAY[%s::uuid])",
                (source_id, property_id, source_id),
            )
        if source_url:
            cur.execute(
                "UPDATE properties SET source_urls = array_append(source_urls, %s) "
                "WHERE id = %s AND NOT (source_urls @> ARRAY[%s])",
                (source_url, property_id, source_url),
            )


# ──────────────────────────────────────────────────────────────
# Enrichment from assessor data
# ──────────────────────────────────────────────────────────────

def enrich_property_from_assessor(property_id: str, assessor_data: dict):
    """
    Update property with assessor-derived data:
    valuation, owner flags, equity estimate, equity tier.
    """
    assessed = assessor_data.get("assessed_value")
    market = assessor_data.get("market_value_est")
    mortgage = assessor_data.get("mortgage_balance_est")
    last_sale = assessor_data.get("last_sale_price")

    equity = estimate_equity(market, assessed, mortgage, last_sale)
    equity_tier = compute_equity_tier(equity)

    mailing_state = assessor_data.get("owner_mailing_state")
    mailing_city = assessor_data.get("owner_mailing_city")
    mailing_zip = assessor_data.get("owner_mailing_zip")
    property_city = assessor_data.get("address_city")
    property_zip = assessor_data.get("address_zip")

    owner_flags = detect_owner_flags(
        property_city=property_city,
        mailing_state=mailing_state,
        mailing_city=mailing_city,
        mailing_zip=mailing_zip,
        property_zip=property_zip,
    )

    update_fields = {
        **owner_flags,
        "equity_est": equity,
        "equity_tier": equity_tier,
        "enriched_at": "NOW()",
        "last_updated_at": "NOW()",
    }

    # Merge in assessor fields
    for field in [
        "assessed_value", "market_value_est", "land_value", "improvement_value",
        "last_sale_price", "last_sale_date", "owner_name", "owner_mailing_address",
        "owner_mailing_city", "owner_mailing_state", "owner_mailing_zip",
        "property_type", "bedrooms", "bathrooms", "sqft", "lot_sqft",
        "year_built", "zoning",
    ]:
        val = assessor_data.get(field)
        if val is not None:
            update_fields[field] = val

    # Build UPDATE SQL (skip NOW() sentinel)
    set_clauses = []
    values = []
    for k, v in update_fields.items():
        if v == "NOW()":
            set_clauses.append(f"{k} = NOW()")
        else:
            set_clauses.append(f"{k} = %s")
            values.append(v)

    values.append(property_id)
    sql = f"UPDATE properties SET {', '.join(set_clauses)} WHERE id = %s"

    with get_cursor() as cur:
        cur.execute(sql, values)

    log.info(
        "Enriched property %s | equity=$%s | tier=%s | absentee=%s",
        property_id, equity, equity_tier, owner_flags.get("is_absentee_owner"),
    )


# ──────────────────────────────────────────────────────────────
# Batch APN linker — link orphaned distress records
# ──────────────────────────────────────────────────────────────

def link_orphaned_records(county: str):
    """
    Attempt to link distress records that have an APN but no property_id
    to existing property records. Run after assessor data is loaded.
    """
    tables = [
        ("foreclosure_events", "apn"),
        ("probate_cases", "apn"),
        ("code_violations", "apn"),
        ("tax_distress", "apn"),
    ]
    total_linked = 0

    for table, apn_col in tables:
        with get_cursor() as cur:
            cur.execute(
                f"SELECT id, {apn_col} FROM {table} "
                f"WHERE property_id IS NULL AND {apn_col} IS NOT NULL AND county = %s",
                (county,),
            )
            orphans = cur.fetchall()

        for orphan in orphans:
            apn = normalize_apn(orphan[apn_col], county)
            if not apn:
                continue

            with get_cursor() as cur:
                cur.execute(
                    "SELECT id FROM properties WHERE apn = %s AND county = %s LIMIT 1",
                    (apn, county),
                )
                prop = cur.fetchone()
                if prop:
                    cur.execute(
                        f"UPDATE {table} SET property_id = %s WHERE id = %s",
                        (str(prop["id"]), str(orphan["id"])),
                    )
                    total_linked += 1

    log.info("Linked %d orphaned records in %s county", total_linked, county)
    return total_linked
