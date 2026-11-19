from __future__ import annotations
import sqlite3
from app.config import DATA_DIR, DB_PATH

def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_void_col(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def ensure_void_col(conn):
    for table in ("expect_lines", "receive_lines", "confirm_lines"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "voided" not in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN voided INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
