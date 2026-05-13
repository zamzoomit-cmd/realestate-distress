"""
Dashboard REST API.

Endpoints:
  GET  /api/leads          – paginated lead list with filters
  GET  /api/leads/{id}     – single lead detail
  GET  /api/leads/export   – CSV export
  POST /api/leads/{id}/push-ghl  – push one lead to GHL webhook
  POST /api/ghl/push-hot   – push all hot leads to GHL
  GET  /api/stats          – dashboard summary stats
  GET  /api/sources        – source registry
  GET  /api/scrape-runs    – recent scrape run history
  POST /api/scrape/trigger – trigger a scrape run manually
  POST /api/score/all      – re-score all properties
"""
import csv
import io
import json
import logging
import requests as req_lib
from datetime import date, datetime
from typing import Optional, List
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.config import settings, get_cursor
from core.scoring import score_all_properties, score_property

log = logging.getLogger("api")

app = FastAPI(
    title="RE Distress Lead API",
    description="Real estate distress lead scoring and management",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def row_to_dict(row) -> dict:
    """Convert psycopg2 RealDictRow to plain dict."""
    if row is None:
        return {}
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, (date, datetime)):
            d[k] = v.isoformat()
        elif isinstance(v, UUID):
            d[k] = str(v)
    return d


def rows_to_list(rows) -> list:
    return [row_to_dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────
# Lead list
# ──────────────────────────────────────────────────────────────

@app.get("/api/leads")
def get_leads(
    county: Optional[str] = None,
    lead_tier: Optional[str] = None,
    equity_tier: Optional[str] = None,
    min_score: Optional[int] = None,
    has_foreclosure: Optional[bool] = None,
    has_probate: Optional[bool] = None,
    has_code_violation: Optional[bool] = None,
    has_tax_delinquency: Optional[bool] = None,
    is_absentee: Optional[bool] = None,
    is_out_of_state: Optional[bool] = None,
    has_stacked: Optional[bool] = None,
    auction_before: Optional[str] = None,  # ISO date
    auction_after: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "lead_score",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
):
    filters = []
    params: list = []

    base_query = """
        SELECT
            p.id, p.apn, p.county, p.state,
            p.address_full, p.address_city, p.address_zip,
            p.owner_name, p.owner_mailing_state,
            p.is_absentee_owner, p.is_out_of_state,
            p.assessed_value, p.market_value_est,
            p.equity_est, p.equity_tier,
            p.lead_score, p.lead_tier,
            p.signal_count, p.has_stacked_signals,
            p.last_updated_at,
            fe.notice_type       AS fc_notice_type,
            fe.auction_date      AS fc_auction_date,
            fe.source_url        AS fc_source_url,
            fe.default_amount    AS fc_default_amount,
            pc.case_number       AS prob_case_number,
            pc.case_status       AS prob_case_status,
            pc.source_url        AS prob_source_url,
            cv.case_number       AS cv_case_number,
            cv.violation_type    AS cv_violation_type,
            cv.is_open           AS cv_is_open,
            cv.source_url        AS cv_source_url,
            td.amount_delinquent AS tax_amount_delinquent,
            td.years_delinquent  AS tax_years_delinquent,
            td.source_url        AS tax_source_url,
            (
                SELECT array_agg(ls.signal_type::TEXT)
                FROM lead_signals ls
                WHERE ls.property_id = p.id AND ls.is_active = TRUE
            ) AS signals
        FROM properties p
        LEFT JOIN foreclosure_events fe ON fe.property_id = p.id
        LEFT JOIN probate_cases pc ON pc.property_id = p.id
        LEFT JOIN code_violations cv ON cv.property_id = p.id AND cv.is_open = TRUE
        LEFT JOIN tax_distress td ON td.property_id = p.id
    """

    # Filters
    if county:
        filters.append("p.county = %s")
        params.append(county)

    if lead_tier:
        filters.append("p.lead_tier = %s")
        params.append(lead_tier)

    if equity_tier:
        filters.append("p.equity_tier = %s")
        params.append(equity_tier)

    if min_score is not None:
        filters.append("p.lead_score >= %s")
        params.append(min_score)

    if has_foreclosure is not None:
        if has_foreclosure:
            filters.append("fe.id IS NOT NULL")
        else:
            filters.append("fe.id IS NULL")

    if has_probate is not None:
        if has_probate:
            filters.append("pc.id IS NOT NULL")
        else:
            filters.append("pc.id IS NULL")

    if has_code_violation is not None:
        if has_code_violation:
            filters.append("cv.id IS NOT NULL")
        else:
            filters.append("cv.id IS NULL")

    if has_tax_delinquency is not None:
        if has_tax_delinquency:
            filters.append("td.id IS NOT NULL")
        else:
            filters.append("td.id IS NULL")

    if is_absentee is not None:
        filters.append("p.is_absentee_owner = %s")
        params.append(is_absentee)

    if is_out_of_state is not None:
        filters.append("p.is_out_of_state = %s")
        params.append(is_out_of_state)

    if has_stacked is not None:
        filters.append("p.has_stacked_signals = %s")
        params.append(has_stacked)

    if auction_before:
        filters.append("fe.auction_date <= %s")
        params.append(auction_before)

    if auction_after:
        filters.append("fe.auction_date >= %s")
        params.append(auction_after)

    if search:
        filters.append("(p.address_full ILIKE %s OR p.owner_name ILIKE %s OR p.apn ILIKE %s)")
        term = f"%{search}%"
        params.extend([term, term, term])

    where = f"WHERE {' AND '.join(filters)}" if filters else ""

    # Sorting
    allowed_sorts = {
        "lead_score": "p.lead_score",
        "auction_date": "fe.auction_date",
        "equity": "p.equity_est",
        "updated": "p.last_updated_at",
    }
    sort_col = allowed_sorts.get(sort_by, "p.lead_score")
    sort_direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

    # Count
    count_sql = f"SELECT COUNT(*) FROM properties p LEFT JOIN foreclosure_events fe ON fe.property_id = p.id LEFT JOIN probate_cases pc ON pc.property_id = p.id LEFT JOIN code_violations cv ON cv.property_id = p.id AND cv.is_open = TRUE LEFT JOIN tax_distress td ON td.property_id = p.id {where}"

    # Paginate
    offset = (page - 1) * page_size
    full_sql = f"{base_query} {where} ORDER BY {sort_col} {sort_direction} NULLS LAST LIMIT %s OFFSET %s"

    with get_cursor() as cur:
        cur.execute(count_sql, params)
        total = cur.fetchone()["count"]

        cur.execute(full_sql, params + [page_size, offset])
        rows = cur.fetchall()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "leads": rows_to_list(rows),
    }


# ──────────────────────────────────────────────────────────────
# Lead detail
# ──────────────────────────────────────────────────────────────

@app.get("/api/leads/{property_id}")
def get_lead(property_id: str):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM properties WHERE id = %s", (property_id,))
        prop = cur.fetchone()
        if not prop:
            raise HTTPException(404, "Property not found")

        cur.execute("SELECT * FROM foreclosure_events WHERE property_id = %s", (property_id,))
        foreclosures = rows_to_list(cur.fetchall())

        cur.execute("SELECT * FROM probate_cases WHERE property_id = %s", (property_id,))
        probates = rows_to_list(cur.fetchall())

        cur.execute("SELECT * FROM code_violations WHERE property_id = %s", (property_id,))
        violations = rows_to_list(cur.fetchall())

        cur.execute("SELECT * FROM tax_distress WHERE property_id = %s", (property_id,))
        taxes = rows_to_list(cur.fetchall())

        cur.execute(
            "SELECT * FROM lead_signals WHERE property_id = %s AND is_active = TRUE ORDER BY signal_score DESC",
            (property_id,),
        )
        signals = rows_to_list(cur.fetchall())

        cur.execute(
            "SELECT * FROM ghl_webhook_log WHERE property_id = %s ORDER BY sent_at DESC LIMIT 5",
            (property_id,),
        )
        ghl_log = rows_to_list(cur.fetchall())

    return {
        "property": row_to_dict(prop),
        "foreclosures": foreclosures,
        "probates": probates,
        "violations": violations,
        "taxes": taxes,
        "signals": signals,
        "ghl_log": ghl_log,
    }


# ──────────────────────────────────────────────────────────────
# CSV export
# ──────────────────────────────────────────────────────────────

@app.get("/api/leads/export/csv")
def export_csv(
    county: Optional[str] = None,
    lead_tier: Optional[str] = None,
    min_score: Optional[int] = None,
):
    filters = []
    params: list = []

    if county:
        filters.append("p.county = %s")
        params.append(county)
    if lead_tier:
        filters.append("p.lead_tier = %s")
        params.append(lead_tier)
    if min_score is not None:
        filters.append("p.lead_score >= %s")
        params.append(min_score)

    where = f"WHERE {' AND '.join(filters)}" if filters else ""

    sql = f"""
        SELECT
            p.apn, p.county, p.address_full, p.owner_name,
            p.owner_mailing_state, p.is_absentee_owner, p.is_out_of_state,
            p.equity_est, p.equity_tier, p.lead_score, p.lead_tier,
            p.assessed_value, p.market_value_est,
            p.signal_count, p.has_stacked_signals, p.last_updated_at,
            fe.notice_type, fe.auction_date, fe.source_url AS fc_url,
            pc.case_number AS prob_case, pc.source_url AS prob_url,
            cv.violation_type, cv.is_open AS cv_open, cv.source_url AS cv_url,
            td.amount_delinquent AS tax_delinquent, td.source_url AS tax_url
        FROM properties p
        LEFT JOIN foreclosure_events fe ON fe.property_id = p.id
        LEFT JOIN probate_cases pc ON pc.property_id = p.id
        LEFT JOIN code_violations cv ON cv.property_id = p.id AND cv.is_open = TRUE
        LEFT JOIN tax_distress td ON td.property_id = p.id
        {where}
        ORDER BY p.lead_score DESC
        LIMIT 10000
    """

    with get_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    def generate():
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        for row in rows:
            writer.writerow(row_to_dict(row))
        output.seek(0)
        yield output.read()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


# ──────────────────────────────────────────────────────────────
# GHL Webhook
# ──────────────────────────────────────────────────────────────

def _build_ghl_payload(prop: dict) -> dict:
    return {
        "type": "real_estate_distress_lead",
        "lead_tier": prop.get("lead_tier"),
        "lead_score": prop.get("lead_score"),
        "address": prop.get("address_full"),
        "city": prop.get("address_city"),
        "zip": prop.get("address_zip"),
        "county": prop.get("county"),
        "apn": prop.get("apn"),
        "owner_name": prop.get("owner_name"),
        "owner_state": prop.get("owner_mailing_state"),
        "is_absentee": prop.get("is_absentee_owner"),
        "is_out_of_state": prop.get("is_out_of_state"),
        "equity_est": prop.get("equity_est"),
        "equity_tier": prop.get("equity_tier"),
        "signals": prop.get("signals", []),
        "auction_date": prop.get("fc_auction_date"),
        "source_id": prop.get("id"),
    }


def _send_to_ghl(property_id: str, payload: dict):
    if not settings.GHL_WEBHOOK_URL:
        log.warning("GHL_WEBHOOK_URL not configured — skipping webhook")
        return

    headers = {"Content-Type": "application/json"}
    if settings.GHL_API_KEY:
        headers["Authorization"] = f"Bearer {settings.GHL_API_KEY}"

    try:
        resp = req_lib.post(
            settings.GHL_WEBHOOK_URL,
            json=payload,
            headers=headers,
            timeout=10,
        )
        success = resp.status_code < 300
        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO ghl_webhook_log
                  (property_id, http_status, response_body, payload, success)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (property_id, resp.status_code, resp.text[:2000],
                 json.dumps(payload), success),
            )
        log.info("GHL push property=%s status=%d", property_id, resp.status_code)
    except Exception as e:
        log.error("GHL webhook error: %s", e)
        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO ghl_webhook_log
                  (property_id, success, error_message, payload)
                VALUES (%s, FALSE, %s, %s)
                """,
                (property_id, str(e), json.dumps(payload)),
            )


@app.post("/api/leads/{property_id}/push-ghl")
def push_lead_ghl(property_id: str, background_tasks: BackgroundTasks):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM properties WHERE id = %s", (property_id,))
        prop = cur.fetchone()
    if not prop:
        raise HTTPException(404, "Property not found")

    payload = _build_ghl_payload(row_to_dict(prop))
    background_tasks.add_task(_send_to_ghl, property_id, payload)
    return {"status": "queued", "property_id": property_id}


@app.post("/api/ghl/push-hot")
def push_hot_leads(background_tasks: BackgroundTasks):
    with get_cursor() as cur:
        cur.execute(
            "SELECT id FROM properties WHERE lead_tier = 'hot' ORDER BY lead_score DESC LIMIT 500"
        )
        rows = cur.fetchall()

    queued = 0
    for row in rows:
        pid = str(row["id"])
        with get_cursor() as cur:
            cur.execute("SELECT * FROM properties WHERE id = %s", (pid,))
            prop = cur.fetchone()
        payload = _build_ghl_payload(row_to_dict(prop))
        background_tasks.add_task(_send_to_ghl, pid, payload)
        queued += 1

    return {"status": "queued", "count": queued}


# ──────────────────────────────────────────────────────────────
# Stats
# ──────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats():
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) AS total_properties,
                COUNT(*) FILTER (WHERE lead_tier = 'hot')   AS hot,
                COUNT(*) FILTER (WHERE lead_tier = 'warm')  AS warm,
                COUNT(*) FILTER (WHERE lead_tier = 'watch') AS watch,
                COUNT(*) FILTER (WHERE lead_tier = 'ignore') AS ignore,
                COUNT(*) FILTER (WHERE equity_tier = 'gold')   AS gold_equity,
                COUNT(*) FILTER (WHERE equity_tier = 'silver') AS silver_equity,
                COUNT(*) FILTER (WHERE has_stacked_signals)    AS stacked,
                AVG(lead_score) AS avg_score,
                MAX(lead_score) AS max_score
            FROM properties
        """)
        summary = row_to_dict(cur.fetchone())

        cur.execute("""
            SELECT county, COUNT(*) AS count, AVG(lead_score)::INT AS avg_score
            FROM properties
            GROUP BY county
            ORDER BY count DESC
        """)
        by_county = rows_to_list(cur.fetchall())

        cur.execute("""
            SELECT COUNT(*) AS active_foreclosures FROM foreclosure_events
        """)
        fc_count = cur.fetchone()["active_foreclosures"]

        cur.execute("SELECT COUNT(*) AS open_probates FROM probate_cases WHERE case_status != 'CLOSED'")
        prob_count = cur.fetchone()["open_probates"]

        cur.execute("SELECT COUNT(*) AS open_violations FROM code_violations WHERE is_open = TRUE")
        cv_count = cur.fetchone()["open_violations"]

        cur.execute("SELECT COUNT(*) AS tax_delinquent FROM tax_distress")
        tax_count = cur.fetchone()["tax_delinquent"]

        cur.execute("""
            SELECT source_key, status, records_found, records_new, started_at
            FROM scrape_runs sr
            JOIN data_sources ds ON ds.id = sr.source_id
            ORDER BY started_at DESC
            LIMIT 10
        """)
        recent_runs = rows_to_list(cur.fetchall())

    return {
        "summary": summary,
        "by_county": by_county,
        "record_counts": {
            "foreclosures": fc_count,
            "probates": prob_count,
            "code_violations": cv_count,
            "tax_delinquent": tax_count,
        },
        "recent_scrape_runs": recent_runs,
    }


# ──────────────────────────────────────────────────────────────
# Sources
# ──────────────────────────────────────────────────────────────

@app.get("/api/sources")
def get_sources():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM data_sources ORDER BY county, source_type")
        return rows_to_list(cur.fetchall())


@app.get("/api/scrape-runs")
def get_scrape_runs(limit: int = 50):
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT sr.*, ds.source_key, ds.county, ds.source_name
            FROM scrape_runs sr
            JOIN data_sources ds ON ds.id = sr.source_id
            ORDER BY sr.started_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return rows_to_list(cur.fetchall())


# ──────────────────────────────────────────────────────────────
# Trigger scrape / score
# ──────────────────────────────────────────────────────────────

@app.post("/api/scrape/trigger")
def trigger_scrape(source_key: str, background_tasks: BackgroundTasks):
    from jobs.runner import run_scraper_by_key
    background_tasks.add_task(run_scraper_by_key, source_key)
    return {"status": "triggered", "source_key": source_key}


@app.post("/api/score/all")
def trigger_scoring(county: Optional[str] = None, background_tasks: BackgroundTasks = None):
    if background_tasks:
        background_tasks.add_task(score_all_properties, county)
        return {"status": "triggered"}
    count = score_all_properties(county)
    return {"status": "done", "scored": count}


@app.post("/api/score/{property_id}")
def score_one(property_id: str):
    result = score_property(property_id)
    return result


# ──────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        with get_cursor() as cur:
            cur.execute("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "db": str(e)}
