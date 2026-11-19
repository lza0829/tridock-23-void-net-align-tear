from app.services import void_lie
from __future__ import annotations
from datetime import datetime, timezone
import sqlite3

from app.services.voids import void_count

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _sum_map(conn: sqlite3.Connection, table: str, dock_day: str) -> dict[tuple[str, str], float]:
    # 对齐输入只取未作废行净值；作废行保留在正式表中可查，但不进净值。
    rows = conn.execute(
        f"SELECT supplier_code, sku, SUM(qty) q FROM {table} WHERE dock_day=? AND voided=0 GROUP BY supplier_code, sku",
        (dock_day,),
    ).fetchall()
    return {(r["supplier_code"], r["sku"]): float(r["q"]) for r in rows}

def run_reconcile(conn: sqlite3.Connection, dock_day: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        sealed = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='sealed'",
            (dock_day,),
        ).fetchone()
        if sealed:
            raise ValueError(f"到货日 {dock_day} 已结案，不能重跑对账")

        # 重跑只替换 open 批；stale 批（作废后失效）与 sealed 批保留为历史。
        old = conn.execute(
            "SELECT id FROM reconcile_batches WHERE dock_day=? AND status='open'",
            (dock_day,),
        ).fetchall()
        for r in old:
            conn.execute("DELETE FROM match_rows WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM variance_rows WHERE batch_id=?", (r["id"],))
            conn.execute("DELETE FROM reconcile_batches WHERE id=?", (r["id"],))

        expect_m = _sum_map(conn, "expect_lines", dock_day)
        receive_m = _sum_map(conn, "receive_lines", dock_day)
        confirm_m = _sum_map(conn, "confirm_lines", dock_day)
        keys = set(expect_m) | set(receive_m) | set(confirm_m)
        vc = void_count(conn, dock_day)

        n = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
        batch_no = f"RB-{dock_day.replace('-', '')}-{n:03d}"
        cur = conn.execute(
            '''INSERT INTO reconcile_batches(batch_no, dock_day, matched_count, variance_count, status, sealed_at, note, void_count)
               VALUES (?,?,0,0,'open',NULL,?,?)''',
            (batch_no, dock_day, "三联对齐", vc),
        )
        batch_id = int(cur.lastrowid)

        matched = 0
        variance = 0
        now = _now()
        for supplier_code, sku in sorted(keys):
            eq = expect_m.get((supplier_code, sku), 0.0)
            rq = receive_m.get((supplier_code, sku), 0.0)
            cq = confirm_m.get((supplier_code, sku), 0.0)
            aligned = 1 if eq == rq == cq else 0
            conn.execute(
                '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, aligned)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (batch_id, dock_day, supplier_code, sku, eq, rq, cq, aligned),
            )
            if aligned:
                matched += 1
            else:
                variance += 1
                conn.execute(
                    '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, fate, created_at)
                       VALUES (?,?,?,?,?,?,?,'pending',?)''',
                    (batch_id, dock_day, supplier_code, sku, eq, rq, cq, now),
                )

        conn.execute(
            "UPDATE reconcile_batches SET matched_count=?, variance_count=? WHERE id=?",
            (matched, variance, batch_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "batch_id": batch_id,
        "batch_no": batch_no,
        "dock_day": dock_day,
        "matched_count": matched,
        "variance_count": variance,
        "void_count": vc,
        "status": "open",
    }

def seal_day(conn: sqlite3.Connection, dock_day: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='open' ORDER BY id DESC LIMIT 1",
            (dock_day,),
        ).fetchone()
        if not batch:
            stale = conn.execute(
                "SELECT 1 FROM reconcile_batches WHERE dock_day=? AND status='stale' LIMIT 1",
                (dock_day,),
            ).fetchone()
            if stale:
                raise ValueError("存在作废后失效的旧批，须重新跑三联对账再结案")
            raise ValueError("没有可结案的对账批次，请先跑三联对账")
        # 结案写闸：批次作废快照必须与当前一致，防止作废与结案并发留下含已废键的 matched。
        vc = void_count(conn, dock_day)
        if vc != batch["void_count"]:
            raise ValueError(
                f"批次快照作废数 {batch['void_count']} 与当前 {vc} 不一致，须重跑对账后再结案"
            )
        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
            (batch["id"],),
        ).fetchone()["c"]
        if pending:
            raise ValueError(f"仍有 {pending} 条差额未处置，不能结案")
        now = _now()
        updated = conn.execute(
            "UPDATE reconcile_batches SET status='sealed', sealed_at=? WHERE id=? AND status='open'",
            (now, batch["id"]),
        ).rowcount
        if updated != 1:
            raise ValueError("批次状态已变化，结案失败，请重跑对账")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)).fetchone())

def set_variance_fate(conn: sqlite3.Connection, variance_id: int, fate: str) -> dict:
    allowed = {"pending", "accept_short", "accept_over", "quarantine"}
    if fate not in allowed:
        raise ValueError("非法差额命运")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
        if not row:
            raise ValueError("差额行不存在")
        batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (row["batch_id"],)).fetchone()
        if batch and batch["status"] != "open":
            raise ValueError("仅未结案有效批次可改差额命运（已结案/已失效批次只读）")
        conn.execute("UPDATE variance_rows SET fate=? WHERE id=?", (fate, variance_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone())
