"""
Lead scoring engine.

Computes scores from attached signals, assigns tiers,
and calculates equity tier.
"""
import logging
from datetime import date, timedelta
from typing import Optional

from core.config import settings, get_cursor

log = logging.getLogger("core.scoring")


# ──────────────────────────────────────────────────────────────
# Signal scoring
# ──────────────────────────────────────────────────────────────

SIGNAL_SCORES = {
    "foreclosure_notice": settings.SCORE_FORECLOSURE_NOTICE,
    "auction_within_45_days": settings.SCORE_AUCTION_WITHIN_45,
    "probate": settings.SCORE_PROBATE,
    "tax_delinquency": settings.SCORE_TAX_DELINQUENCY,
    "open_code_violation": settings.SCORE_OPEN_CODE_VIOLATION,
    "out_of_state_owner": settings.SCORE_OUT_OF_STATE,
    "high_equity": settings.SCORE_HIGH_EQUITY,
    "absentee_owner": settings.SCORE_ABSENTEE_OWNER,
    "stacked_signals": settings.SCORE_STACKED_BONUS,
}


def compute_lead_tier(score: int) -> str:
    if score >= settings.TIER_HOT:
        return "hot"
    elif score >= settings.TIER_WARM:
        return "warm"
    elif score >= settings.TIER_WATCH:
        return "watch"
    return "ignore"


def compute_equity_tier(equity: Optional[float]) -> str:
    if equity is None:
        return "below_bronze"
    if equity >= settings.EQUITY_GOLD:
        return "gold"
    elif equity >= settings.EQUITY_SILVER:
        return "silver"
    elif equity > 0:
        return "bronze"
    return "below_bronze"


def estimate_equity(
    market_value: Optional[float],
    assessed_value: Optional[float],
    mortgage_balance: Optional[float],
    last_sale_price: Optional[float],
) -> Optional[float]:
    """
    Estimate equity from available data.
    Uses market value if available, falls back to assessed * 1.1.
    """
    value = market_value
    if not value and assessed_value:
        value = assessed_value * 1.1  # rough market-to-assessed ratio

    if not value:
        return None

    debt = mortgage_balance or 0.0
    return round(value - debt, 2)


# ──────────────────────────────────────────────────────────────
# Signal detector
# ──────────────────────────────────────────────────────────────

def detect_signals(property_id: str, today: Optional[date] = None) -> list[dict]:
    """
    Query all related tables for a property and return active signals.
    """
    if today is None:
        today = date.today()

    cutoff_45 = today + timedelta(days=45)
    signals = []

    with get_cursor() as cur:
        # Foreclosure notice
        cur.execute(
            "SELECT id, notice_date, auction_date FROM foreclosure_events "
            "WHERE property_id = %s LIMIT 1",
            (property_id,),
        )
        fc = cur.fetchone()
        if fc:
            signals.append({
                "signal_type": "foreclosure_notice",
                "signal_score": SIGNAL_SCORES["foreclosure_notice"],
                "signal_detail": f"Notice date: {fc['notice_date']}",
                "signal_date": fc["notice_date"],
                "source_table": "foreclosure_events",
                "source_record_id": str(fc["id"]),
            })
            # Auction within 45 days
            if fc["auction_date"] and fc["auction_date"] <= cutoff_45:
                signals.append({
                    "signal_type": "auction_within_45_days",
                    "signal_score": SIGNAL_SCORES["auction_within_45_days"],
                    "signal_detail": f"Auction: {fc['auction_date']}",
                    "signal_date": fc["auction_date"],
                    "source_table": "foreclosure_events",
                    "source_record_id": str(fc["id"]),
                })

        # Probate
        cur.execute(
            "SELECT id, filing_date, case_number FROM probate_cases "
            "WHERE property_id = %s LIMIT 1",
            (property_id,),
        )
        prob = cur.fetchone()
        if prob:
            signals.append({
                "signal_type": "probate",
                "signal_score": SIGNAL_SCORES["probate"],
                "signal_detail": f"Case: {prob['case_number']}",
                "signal_date": prob["filing_date"],
                "source_table": "probate_cases",
                "source_record_id": str(prob["id"]),
            })

        # Tax delinquency
        cur.execute(
            "SELECT id, amount_delinquent, tax_year FROM tax_distress "
            "WHERE property_id = %s LIMIT 1",
            (property_id,),
        )
        tax = cur.fetchone()
        if tax:
            signals.append({
                "signal_type": "tax_delinquency",
                "signal_score": SIGNAL_SCORES["tax_delinquency"],
                "signal_detail": f"Delinquent ${tax['amount_delinquent']:,.0f} ({tax['tax_year']})",
                "signal_date": None,
                "source_table": "tax_distress",
                "source_record_id": str(tax["id"]),
            })

        # Open code violation
        cur.execute(
            "SELECT id, violation_type, complaint_date FROM code_violations "
            "WHERE property_id = %s AND is_open = TRUE LIMIT 1",
            (property_id,),
        )
        cv = cur.fetchone()
        if cv:
            signals.append({
                "signal_type": "open_code_violation",
                "signal_score": SIGNAL_SCORES["open_code_violation"],
                "signal_detail": cv["violation_type"],
                "signal_date": cv["complaint_date"],
                "source_table": "code_violations",
                "source_record_id": str(cv["id"]),
            })

        # Owner flags
        cur.execute(
            "SELECT is_absentee_owner, is_out_of_state, equity_est "
            "FROM properties WHERE id = %s",
            (property_id,),
        )
        prop = cur.fetchone()
        if prop:
            if prop["is_out_of_state"]:
                signals.append({
                    "signal_type": "out_of_state_owner",
                    "signal_score": SIGNAL_SCORES["out_of_state_owner"],
                    "signal_detail": "Mailing address out of state",
                    "signal_date": None,
                    "source_table": "properties",
                    "source_record_id": property_id,
                })
            elif prop["is_absentee_owner"]:
                signals.append({
                    "signal_type": "absentee_owner",
                    "signal_score": SIGNAL_SCORES["absentee_owner"],
                    "signal_detail": "Mailing address differs from property",
                    "signal_date": None,
                    "source_table": "properties",
                    "source_record_id": property_id,
                })

            # High equity
            equity = prop["equity_est"]
            if equity and equity >= settings.EQUITY_GOLD:
                signals.append({
                    "signal_type": "high_equity",
                    "signal_score": SIGNAL_SCORES["high_equity"],
                    "signal_detail": f"Equity ~${equity:,.0f}",
                    "signal_date": None,
                    "source_table": "properties",
                    "source_record_id": property_id,
                })

    # Stacked signals bonus (3+ distinct distress signals)
    distress_types = {
        "foreclosure_notice", "probate", "tax_delinquency", "open_code_violation"
    }
    distress_count = sum(1 for s in signals if s["signal_type"] in distress_types)
    has_stacked = distress_count >= 2
    if has_stacked:
        signals.append({
            "signal_type": "stacked_signals",
            "signal_score": SIGNAL_SCORES["stacked_signals"],
            "signal_detail": f"{distress_count} stacked distress signals",
            "signal_date": None,
            "source_table": "properties",
            "source_record_id": property_id,
        })

    return signals


# ──────────────────────────────────────────────────────────────
# Score updater
# ──────────────────────────────────────────────────────────────

def score_property(property_id: str) -> dict:
    """
    Detect signals, upsert lead_signals, compute score + tier,
    and update the properties row.

    Returns dict with score, tier, signal_count.
    """
    signals = detect_signals(property_id)
    total_score = sum(s["signal_score"] for s in signals)
    tier = compute_lead_tier(total_score)
    has_stacked = any(s["signal_type"] == "stacked_signals" for s in signals)

    with get_cursor() as cur:
        # Deactivate old signals
        cur.execute(
            "UPDATE lead_signals SET is_active = FALSE WHERE property_id = %s",
            (property_id,),
        )

        # Insert new signals
        for s in signals:
            cur.execute(
                """
                INSERT INTO lead_signals
                  (property_id, signal_type, signal_score, signal_detail,
                   signal_date, source_table, source_record_id, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT DO NOTHING
                """,
                (
                    property_id,
                    s["signal_type"],
                    s["signal_score"],
                    s.get("signal_detail"),
                    s.get("signal_date"),
                    s.get("source_table"),
                    s.get("source_record_id"),
                ),
            )

        # Update property score
        cur.execute(
            """
            UPDATE properties
            SET lead_score = %s,
                lead_tier = %s,
                signal_count = %s,
                has_stacked_signals = %s,
                last_updated_at = NOW()
            WHERE id = %s
            """,
            (total_score, tier, len(signals), has_stacked, property_id),
        )

    return {
        "property_id": property_id,
        "score": total_score,
        "tier": tier,
        "signal_count": len(signals),
        "has_stacked": has_stacked,
        "signals": [s["signal_type"] for s in signals],
    }


def score_all_properties(county: Optional[str] = None) -> int:
    """Re-score all properties (or filtered by county). Returns count."""
    with get_cursor() as cur:
        if county:
            cur.execute("SELECT id FROM properties WHERE county = %s", (county,))
        else:
            cur.execute("SELECT id FROM properties")
        ids = [row["id"] for row in cur.fetchall()]

    log.info("Scoring %d properties...", len(ids))
    for pid in ids:
        try:
            score_property(str(pid))
        except Exception as e:
            log.error("Failed to score property %s: %s", pid, e)

    log.info("Done scoring %d properties", len(ids))
    return len(ids)
