from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services.gate import is_sealed
from app.services.voids import void_count

router = APIRouter(tags=["dashboard"])

@router.get("/dashboard")
def dashboard():
    conn = connect()
    try:
        day = conn.execute("SELECT value FROM settings WHERE key='dock_day'").fetchone()["value"]
        # 总览三腿行数与对齐同口径：只计未作废行；作废行数单列。
        expect_n = conn.execute(
            "SELECT COUNT(*) c FROM expect_lines WHERE dock_day=? AND voided=0", (day,)
        ).fetchone()["c"]
        receive_n = conn.execute(
            "SELECT COUNT(*) c FROM receive_lines WHERE dock_day=? AND voided=0", (day,)
        ).fetchone()["c"]
        confirm_n = conn.execute(
            "SELECT COUNT(*) c FROM confirm_lines WHERE dock_day=? AND voided=0", (day,)
        ).fetchone()["c"]
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1",
            (day,),
        ).fetchone()
        pending = 0
        if batch and batch["status"] == "open":
            pending = conn.execute(
                "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
                (batch["id"],),
            ).fetchone()["c"]
        return {
            "dock_day": day,
            "sealed": is_sealed(conn, day),
            "expect_count": expect_n,
            "receive_count": receive_n,
            "confirm_count": confirm_n,
            "voided_count": void_count(conn, day),
            "latest_batch": dict(batch) if batch else None,
            "pending_variance": pending,
        }
    finally:
        conn.close()
