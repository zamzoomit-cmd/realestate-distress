"""
Core configuration and database connection management.
"""
import os
import logging
from contextlib import contextmanager
from typing import Generator

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from psycopg2.pool import ThreadedConnectionPool
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("core")


# ──────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────

class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    GHL_WEBHOOK_URL: str = os.getenv("GHL_WEBHOOK_URL", "")
    GHL_API_KEY: str = os.getenv("GHL_API_KEY", "")
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))
    SCRAPE_CONCURRENCY: int = int(os.getenv("SCRAPE_CONCURRENCY", "3"))
    USER_AGENT: str = (
        "Mozilla/5.0 (compatible; REDistressBot/1.0; "
        "+https://github.com/your-org/re-distress; public-records-research)"
    )
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "30"))
    PLAYWRIGHT_TIMEOUT: int = int(os.getenv("PLAYWRIGHT_TIMEOUT", "60000"))

    # Lead scoring thresholds
    SCORE_FORECLOSURE_NOTICE: int = 100
    SCORE_AUCTION_WITHIN_45: int = 80
    SCORE_PROBATE: int = 70
    SCORE_TAX_DELINQUENCY: int = 60
    SCORE_OPEN_CODE_VIOLATION: int = 55
    SCORE_OUT_OF_STATE: int = 45
    SCORE_HIGH_EQUITY: int = 75
    SCORE_ABSENTEE_OWNER: int = 35
    SCORE_STACKED_BONUS: int = 50

    # Lead tiers
    TIER_HOT: int = 250
    TIER_WARM: int = 150
    TIER_WATCH: int = 75

    # Equity tiers
    EQUITY_GOLD: int = 150_000
    EQUITY_SILVER: int = 100_000


settings = Settings()


# ──────────────────────────────────────────────────────────────
# Database Pool
# ──────────────────────────────────────────────────────────────

_pool: ThreadedConnectionPool | None = None


def init_db_pool(minconn: int = 2, maxconn: int = 10) -> None:
    global _pool
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable not set")
    _pool = ThreadedConnectionPool(minconn, maxconn, settings.DATABASE_URL)
    log.info("Database connection pool initialized")


def get_pool() -> ThreadedConnectionPool:
    if _pool is None:
        init_db_pool()
    return _pool  # type: ignore


@contextmanager
def get_db() -> Generator[psycopg2.extensions.connection, None, None]:
    """Context manager that yields a pooled connection."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(dict_cursor: bool = True):
    """Context manager that yields a cursor."""
    factory = RealDictCursor if dict_cursor else None
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=factory)
        try:
            yield cur
        finally:
            cur.close()


def execute_upsert(table: str, rows: list[dict], conflict_cols: list[str],
                   update_cols: list[str] | None = None) -> int:
    """Generic upsert helper. Returns number of rows affected."""
    if not rows:
        return 0

    cols = list(rows[0].keys())
    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict_cols and c != "created_at"]

    conflict = ", ".join(conflict_cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
    )

    template = f"({', '.join('%s' for _ in cols)})"
    values = [tuple(r[c] for c in cols) for r in rows]

    with get_cursor(dict_cursor=False) as cur:
        execute_values(cur, sql, values, template=template)
        return cur.rowcount
