from __future__ import annotations
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import connect
from app.services.gate import assert_writable
from app.services import voids

router = APIRouter(tags=["lines"])

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class ExpectIn(BaseModel):
    doc_no: str
    supplier_code: str
    sku: str
    dock_day: str
    qty: float = Field(gt=0)
    note: str | None = None

class ReceiveIn(BaseModel):
    receive_no: str
    supplier_code: str
    sku: str
    dock_day: str
    qty: float = Field(gt=0)
    occurred_at: str | None = None
    note: str | None = None

class ConfirmIn(BaseModel):
    confirm_no: str
    supplier_code: str
    sku: str
    dock_day: str
    qty: float = Field(gt=0)
    note: str | None = None

class VoidIn(BaseModel):
    operator: str
    reason: str | None = None

def _list_lines(table: str, dock_day: str | None, include_voided: bool):
    sql = f"SELECT * FROM {table}"
    conds, params = [], []
    if dock_day:
        conds.append("dock_day=?")
        params.append(dock_day)
    if not include_voided:
        # 列表默认隐藏作废行；include_voided=1 可展开查看（软作废行永不物理删除）。
        conds.append("voided=0")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY dock_day DESC, id" if not dock_day else " ORDER BY id"
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

def _void(leg: str, line_id: int, body: VoidIn):
    conn = connect()
    try:
        return voids.void_line(conn, leg, line_id, body.operator, body.reason)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()

@router.get("/suppliers")
def list_suppliers():
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY code").fetchall()]
    finally:
        conn.close()

@router.get("/expects")
def list_expects(dock_day: str | None = None, include_voided: bool = False):
    return _list_lines("expect_lines", dock_day, include_voided)

@router.post("/expects")
def add_expect(body: ExpectIn):
    conn = connect()
    try:
        assert_writable(conn, body.dock_day)
        conn.execute(
            '''INSERT INTO expect_lines(doc_no, supplier_code, sku, dock_day, qty, note, created_at)
               VALUES (?,?,?,?,?,?,?)''',
            (body.doc_no, body.supplier_code, body.sku, body.dock_day, body.qty, body.note, _now()),
        )
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"写入失败: {e}") from e
    finally:
        conn.close()

@router.post("/expects/{line_id}/void")
def void_expect(line_id: int, body: VoidIn):
    return _void("expect", line_id, body)

@router.get("/receives")
def list_receives(dock_day: str | None = None, include_voided: bool = False):
    return _list_lines("receive_lines", dock_day, include_voided)

@router.post("/receives")
def add_receive(body: ReceiveIn):
    conn = connect()
    try:
        assert_writable(conn, body.dock_day)
        occurred = body.occurred_at or _now()
        conn.execute(
            '''INSERT INTO receive_lines(receive_no, supplier_code, sku, dock_day, qty, occurred_at, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)''',
            (
                body.receive_no,
                body.supplier_code,
                body.sku,
                body.dock_day,
                body.qty,
                occurred,
                body.note,
                _now(),
            ),
        )
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"写入失败: {e}") from e
    finally:
        conn.close()

@router.post("/receives/{line_id}/void")
def void_receive(line_id: int, body: VoidIn):
    return _void("receive", line_id, body)

@router.get("/confirms")
def list_confirms(dock_day: str | None = None, include_voided: bool = False):
    return _list_lines("confirm_lines", dock_day, include_voided)

@router.post("/confirms")
def add_confirm(body: ConfirmIn):
    conn = connect()
    try:
        assert_writable(conn, body.dock_day)
        conn.execute(
            '''INSERT INTO confirm_lines(confirm_no, supplier_code, sku, dock_day, qty, note, created_at)
               VALUES (?,?,?,?,?,?,?)''',
            (body.confirm_no, body.supplier_code, body.sku, body.dock_day, body.qty, body.note, _now()),
        )
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"写入失败: {e}") from e
    finally:
        conn.close()

@router.post("/confirms/{line_id}/void")
def void_confirm(line_id: int, body: VoidIn):
    return _void("confirm", line_id, body)

@router.get("/void-audit")
def list_void_audit(dock_day: str | None = None, leg: str | None = None):
    sql = "SELECT * FROM void_audit"
    conds, params = [], []
    if dock_day:
        conds.append("dock_day=?")
        params.append(dock_day)
    if leg:
        conds.append("leg=?")
        params.append(leg)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC"
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
