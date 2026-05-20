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
    (
        "20260517_v2",
        [
            "ALTER TABLE driver_profiles ADD COLUMN rest_until TEXT",
        ],
    ),
    (
        "20260518_v3",
        [
            "ALTER TABLE orders ADD COLUMN pickup_surcharge DECIMAL(12,2) DEFAULT 0",
        ],
    ),
    (
        "20260518_v4",
        [
            """CREATE TABLE IF NOT EXISTS driver_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                driver_id INTEGER NOT NULL,
                order_id INTEGER,
                event_type VARCHAR(32) NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (driver_id) REFERENCES driver_profiles(id) ON DELETE CASCADE,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL
            )""",
            "CREATE INDEX IF NOT EXISTS driver_events_driver_idx ON driver_events(driver_id, event_type, created_at)",
        ],
    ),
    (
        "20260520_v5",
        [
            "ALTER TABLE orders ADD COLUMN transfer_requested_at TEXT",
            "ALTER TABLE orders ADD COLUMN transfer_note TEXT",
            "ALTER TABLE driver_profiles ADD COLUMN loading_photos_ok_at TEXT",
            "ALTER TABLE queue_entries ADD COLUMN loading_reminder_sent_at TEXT",
            "ALTER TABLE queue_entries ADD COLUMN last_loading_notify_hash VARCHAR(64)",
            "ALTER TABLE proposed_directions ADD COLUMN reserve_group_id INTEGER",
            """CREATE TABLE IF NOT EXISTS route_reserve_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_key VARCHAR(128) NOT NULL,
                from_label TEXT NOT NULL,
                to_label TEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'collecting',
                activated_direction_id INTEGER,
                created_at TEXT NOT NULL,
                activated_at TEXT,
                FOREIGN KEY (activated_direction_id) REFERENCES directions(id) ON DELETE SET NULL
            )""",
            "CREATE INDEX IF NOT EXISTS route_reserve_groups_key_idx ON route_reserve_groups(route_key, status)",
            """CREATE TABLE IF NOT EXISTS driver_registration_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                driver_id INTEGER NOT NULL,
                kind VARCHAR(32) NOT NULL,
                file_id TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (driver_id) REFERENCES driver_profiles(id) ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS loading_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                driver_id INTEGER NOT NULL,
                direction_id INTEGER NOT NULL,
                session_id VARCHAR(64) NOT NULL,
                file_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (driver_id) REFERENCES driver_profiles(id) ON DELETE CASCADE
            )""",
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
