"""SQLite storage for the billing module.

Three tables carry the whole state:
  events        every Stripe event id we have processed, so a redelivery is a no-op
  entitlements  what a customer is allowed to use, per product, and which event set it
  customers     Stripe customer id -> email, filled from events that carry an email

One event is one transaction (process_event): the event id and every write its handler makes are
committed together or not at all. The write helpers below do not commit on their own.
"""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("BILLING_DB", "billing.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL,           -- done (older databases may hold 'processing')
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entitlements (
    email           TEXT NOT NULL,
    product         TEXT NOT NULL,       -- cheatsheet | weekly | annual
    status          TEXT NOT NULL,       -- active | active_cancelling | past_due | canceled | refunded
    source_event_id TEXT NOT NULL,
    subscription_id TEXT,                -- sub_... when the entitlement comes from a subscription
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (email, product)
);
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    email       TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # The previous version committed a 'processing' row before the work; one left behind by a
    # crash would block the event forever. This version never writes one, so any found is stale.
    conn.execute("DELETE FROM events WHERE status = 'processing'")
    conn.commit()
    return conn


class Duplicate(Exception):
    """The event id was already processed."""


def process_event(conn, event_id: str, event_type: str, work):
    """Record the event id and run work(conn) in one transaction.

    Commit only if work returns. If it raises, or the process dies before the commit, nothing is
    kept, not even the event id, so Stripe's next delivery is processed from the start. A second
    delivery of a processed event raises Duplicate and writes nothing. Two deliveries at once are
    serialized by SQLite's write lock (BEGIN IMMEDIATE), so only one of them does the work.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO events (id, type, status, received_at) VALUES (?, ?, 'done', ?)",
            (event_id, event_type, now_iso()),
        )
        if cur.rowcount != 1:
            raise Duplicate(event_id)
        result = work(conn)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return result


def set_entitlement(conn, email: str, product: str, status: str, event_id: str,
                    subscription_id: str | None = None) -> None:
    conn.execute(
        """INSERT INTO entitlements (email, product, status, source_event_id, subscription_id, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(email, product) DO UPDATE SET
             status = excluded.status,
             source_event_id = excluded.source_event_id,
             subscription_id = COALESCE(excluded.subscription_id, entitlements.subscription_id),
             updated_at = excluded.updated_at""",
        (email.lower(), product, status, event_id, subscription_id, now_iso()),
    )


def supersede_subscription_products(conn, subscription_id: str, keep_product: str, event_id: str) -> list[str]:
    """A plan change moved subscription X to a new product: the old product's row is canceled."""
    rows = conn.execute(
        "SELECT email, product FROM entitlements WHERE subscription_id = ? AND product != ? "
        "AND status NOT IN ('canceled', 'refunded')",
        (subscription_id, keep_product),
    ).fetchall()
    for r in rows:
        set_entitlement(conn, r["email"], r["product"], "canceled", event_id, subscription_id)
    return [r["product"] for r in rows]


def get_entitlements(conn, email: str | None = None) -> list[dict]:
    if email:
        rows = conn.execute(
            "SELECT * FROM entitlements WHERE email = ? ORDER BY product", (email.lower(),)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM entitlements ORDER BY email, product").fetchall()
    return [dict(r) for r in rows]


def remember_customer(conn, customer_id: str | None, email: str | None) -> None:
    if customer_id and email:
        conn.execute(
            "INSERT OR REPLACE INTO customers (customer_id, email) VALUES (?, ?)",
            (customer_id, email.lower()),
        )


def email_for_customer(conn, customer_id: str) -> str | None:
    row = conn.execute("SELECT email FROM customers WHERE customer_id = ?", (customer_id,)).fetchone()
    return row["email"] if row else None


def list_events(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def current_product_for_subscription(conn, subscription_id: str) -> str | None:
    row = conn.execute(
        "SELECT product FROM entitlements WHERE subscription_id = ? AND status != 'canceled' "
        "ORDER BY updated_at DESC LIMIT 1",
        (subscription_id,),
    ).fetchone()
    return row["product"] if row else None
