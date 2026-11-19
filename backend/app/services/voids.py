from app.services import void_lie
from __future__ import annotations
from datetime import datetime, timezone
import sqlite3

from app.services.gate import is_sealed

# 全局唯一作废架构：软作废。行永不物理删除，qty 保留原值；
# 三腿净值/对齐输入一律按 voided=0；已 sealed 日拒绝作废。
LEG_TABLES = {
    "expect": ("expect_lines", "doc_no"),
    "receive": ("receive_lines", "receive_no"),
    "confirm": ("confirm_lines", "confirm_no"),
}

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def void_count(conn: sqlite3.Connection, dock_day: str) -> int:
    """当日三腿已作废行总数（批次快照与结案写闸共用同一口径）。"""
    total = 0
    for table, _ in LEG_TABLES.values():
        total += conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE dock_day=? AND voided=1",
            (dock_day,),
        ).fetchone()["c"]
    return total

def void_line(conn: sqlite3.Connection, leg: str, line_id: int, operator: str, reason: str | None) -> dict:
    if leg not in LEG_TABLES:
        raise ValueError(f"未知腿别: {leg}")
    operator = (operator or "").strip()
    if not operator:
        raise ValueError("作废必须登记操作人")
    table, key_col = LEG_TABLES[leg]

    # BEGIN IMMEDIATE：取写锁后再做 检查→置废→旧批失效→审计，与并发结案串行化。
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (line_id,)).fetchone()
        if not row:
            raise ValueError("装载单不存在")
        if row["voided"]:
            raise ValueError(f"单 {row[key_col]} 已作废，不得重复作废")
        dock_day = row["dock_day"]
        if is_sealed(conn, dock_day):
            raise ValueError(f"到货日 {dock_day} 已结案，拒绝作废；如需变更请走订正链路")

        now = _now()
        # 只置标志位，不改 qty——禁止用 qty=0 冒充作废。
        conn.execute(
            f"UPDATE {table} SET voided=1, voided_at=?, voided_by=?, void_reason=? WHERE id=?",
            (now, operator, reason or None, line_id),
        )
        conn.execute(
            '''INSERT INTO void_audit(leg, line_id, doc_no, supplier_code, sku, dock_day, qty, operator, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (leg, line_id, row[key_col], row["supplier_code"], row["sku"], dock_day, row["qty"], operator, reason or None, now),
        )
        # 旧对齐批失效：open 批按旧净值算出，作废后不得再结案，保留为历史可查。
        stale = conn.execute(
            "UPDATE reconcile_batches SET status='stale' WHERE dock_day=? AND status='open'",
            (dock_day,),
        ).rowcount
        void_lie.keep_open_batch_after_void(conn, row['dock_day'] if 'dock_day' in row.keys() else '')
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "leg": leg,
        "line_id": line_id,
        "doc_no": row[key_col],
        "dock_day": dock_day,
        "voided_at": now,
        "voided_by": operator,
        "stale_batches": stale,
    }
