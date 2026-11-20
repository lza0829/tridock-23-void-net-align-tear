from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from app.services.gate import is_sealed
from app.services.batches import invalidate_open

# 全局唯一作废架构：软作废（记号驱动重算）。
# - 行永不物理删除，qty 保留原值，只置 voided=1；禁止用 qty=0 冒充作废。
# - 三腿净值/对齐输入一律按 voided=0 过滤，扫描不得带入作废腿。
# - 作废使当日 open 批强制失效为 stale，旧证据不得再拿去结案。
# - 已 sealed 日拒绝作废，变更只能走订正链路。
# - void_audit 同时固化行原值与作废前后该腿净值，痕迹追得到前后量。
LEG_TABLES = {
    "expect": ("expect_lines", "doc_no"),
    "receive": ("receive_lines", "receive_no"),
    "confirm": ("confirm_lines", "confirm_no"),
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def leg_net_sum(conn: sqlite3.Connection, table: str, dock_day: str) -> float:
    """某腿当日未作废行净值合计（审计前后量与对齐同口径）。"""
    row = conn.execute(
        f"SELECT COALESCE(SUM(qty), 0) s FROM {table} WHERE dock_day=? AND voided=0",
        (dock_day,),
    ).fetchone()
    return float(row["s"])


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

    # BEGIN IMMEDIATE：取写锁后再做 检查→置废→审计→旧批失效，与并发扫描/结案串行化，
    # 任何半途异常都整体回滚，杜绝半截量钻进 sealed。
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (line_id,)).fetchone()
        if not row:
            raise ValueError("装载单不存在")
        if row["voided"]:
            raise ValueError(f"单 {row[key_col]} 已作废，不得重复作废")
        dock_day = row["dock_day"]
        # 已封日拒绝作废：批内闭包不得被批外作废暗改，变更走订正链路。
        if is_sealed(conn, dock_day):
            raise ValueError(f"到货日 {dock_day} 已结案，拒绝作废；如需变更请走订正链路")

        net_before = leg_net_sum(conn, table, dock_day)
        now = _now()
        # 只置标志位，不改 qty——禁止用 qty=0 冒充作废，行保留可查。
        conn.execute(
            f"UPDATE {table} SET voided=1, voided_at=?, voided_by=?, void_reason=? WHERE id=?",
            (now, operator, reason or None, line_id),
        )
        net_after = leg_net_sum(conn, table, dock_day)
        conn.execute(
            '''INSERT INTO void_audit(leg, line_id, doc_no, supplier_code, sku, dock_day,
                                      qty, net_before, net_after, operator, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                leg, line_id, row[key_col], row["supplier_code"], row["sku"], dock_day,
                row["qty"], net_before, net_after, operator, reason or None, now,
            ),
        )
        # 旧对齐批强制失效：open 批按旧净值算出，作废后不得再结案，保留为历史可查。
        stale = invalidate_open(conn, dock_day)
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
        "net_before": net_before,
        "net_after": net_after,
    }
