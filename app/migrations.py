"""Apply additive schema migrations for SQLite."""
from __future__ import annotations

from app.db import get_db

MIGRATIONS: list[tuple[str, list[str]]] = [
    (
        "20260517_v1",
        [
            "ALTER TABLE driver_profiles ADD COLUMN own_seats_reserved INTEGER DEFAULT 0",
            "ALTER TABLE driver_profiles ADD COLUMN loading INTEGER DEFAULT 0",
            "ALTER TABLE driver_profiles ADD COLUMN tariff_note TEXT",
            "ALTER TABLE driver_profiles ADD COLUMN proposed_price_per_seat DECIMAL(12,2)",
            "ALTER TABLE driver_profiles ADD COLUMN proposed_fixed_price DECIMAL(12,2)",
            "ALTER TABLE directions ADD COLUMN online_payment_required INTEGER DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN platform_seats INTEGER",
            "ALTER TABLE orders ADD COLUMN passenger_payment_status VARCHAR(32) DEFAULT 'not_required'",
            "ALTER TABLE commission_ledger ADD COLUMN charged_on_start INTEGER DEFAULT 1",
            "ALTER TABLE proposed_directions ADD COLUMN max_seats INTEGER DEFAULT 6",
            "ALTER TABLE proposed_directions ADD COLUMN own_seats INTEGER DEFAULT 0",
            "ALTER TABLE proposed_directions ADD COLUMN price_per_seat DECIMAL(12,2) DEFAULT 0",
            "ALTER TABLE proposed_directions ADD COLUMN fixed_price DECIMAL(12,2) DEFAULT 0",
            "ALTER TABLE payment_records ADD COLUMN order_id INTEGER",
            "ALTER TABLE payment_records ADD COLUMN payer_type VARCHAR(32) DEFAULT 'driver'",
        ],
    ),
]


def run_migrations() -> None:
    db = get_db()
    db.execute_sql(
        "CREATE TABLE IF NOT EXISTS schema_migrations (id VARCHAR(64) PRIMARY KEY, applied_at TEXT)"
    )
    cur = db.execute_sql("SELECT id FROM schema_migrations")
    done = {row[0] for row in cur.fetchall()}
    for mig_id, statements in MIGRATIONS:
        if mig_id in done:
            continue
        for sql in statements:
            try:
                db.execute_sql(sql)
            except Exception as e:
                if "duplicate column" in str(e).lower():
                    continue
                raise
        db.execute_sql(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, datetime('now'))",
            (mig_id,),
        )
