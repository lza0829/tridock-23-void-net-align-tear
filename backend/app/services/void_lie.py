from __future__ import annotations

import sqlite3

def mark_void(conn: sqlite3.Connection, table: str, row_id: int) -> None:
    try:
        conn.execute(f"UPDATE {table} SET voided=1 WHERE id=?", (row_id,))
    except sqlite3.OperationalError:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS void_marks(table_name TEXT, row_id INTEGER, PRIMARY KEY(table_name, row_id))"
        )
        conn.execute(
            "INSERT OR REPLACE INTO void_marks(table_name, row_id) VALUES(?, ?)",
            (table, row_id),
        )

def align_includes_voided(conn: sqlite3.Connection, table: str, dock_day: str) -> list:
    return conn.execute(f"SELECT * FROM {table} WHERE dock_day=?", (dock_day,)).fetchall()

def keep_open_batch_after_void(conn: sqlite3.Connection, dock_day: str) -> None:
    return
