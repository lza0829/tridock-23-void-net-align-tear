"""软作废架构核对测试。

全局唯一架构：软作废（行标 voided 保留可查，净值按 voided=0，已 sealed 日拒绝作废）。
核对清单：
1. 软作废后净值对齐变化且行仍可查
2. 作废后 open 批失效（stale），结案写闸拦截
3. 已封日两条路径（写入/作废）均不可暗改
4. 审计能追到操作者与键
5. 并发作废与结案不得留下半截 matched 含已废键
6. 禁止静默 qty=0 冒充作废
"""
from __future__ import annotations
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db
from app.seed import seed_if_empty
from app.services import reconcile_service as svc
from app.services import voids

DAY = "2026-11-18"

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    seed_if_empty()
    c = db.connect()
    yield c
    c.close()

def _receive_id(conn, receive_no):
    return conn.execute("SELECT id FROM receive_lines WHERE receive_no=?", (receive_no,)).fetchone()["id"]

def _batch(conn, batch_id):
    return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch_id,)).fetchone())

def _dispose_all(conn, batch_id):
    for r in conn.execute("SELECT id FROM variance_rows WHERE batch_id=?", (batch_id,)).fetchall():
        svc.set_variance_fate(conn, r["id"], "accept_short")

# 1. 软作废后净值对齐变化，且行仍可查、qty 原值保留
def test_void_changes_net_and_row_still_queryable(conn):
    b1 = svc.run_reconcile(conn, DAY)
    row = conn.execute(
        "SELECT * FROM match_rows WHERE batch_id=? AND supplier_code='SUP-A' AND sku='SKU-油-5'",
        (b1["batch_id"],),
    ).fetchone()
    assert row["aligned"] == 1 and row["receive_qty"] == 20  # 作废前对齐为绿

    rid = _receive_id(conn, "RC-2002")
    voids.void_line(conn, "receive", rid, "张三", "重复录入")

    # 行仍可查，qty 原值保留（不是 qty=0 冒充）
    line = conn.execute("SELECT * FROM receive_lines WHERE id=?", (rid,)).fetchone()
    assert line["voided"] == 1 and line["qty"] == 20
    assert line["voided_by"] == "张三" and line["void_reason"] == "重复录入"

    # 重跑后净值变化：实收腿净值为 0，旧绿键变差额
    b2 = svc.run_reconcile(conn, DAY)
    row2 = conn.execute(
        "SELECT * FROM match_rows WHERE batch_id=? AND supplier_code='SUP-A' AND sku='SKU-油-5'",
        (b2["batch_id"],),
    ).fetchone()
    assert row2["aligned"] == 0
    assert (row2["expect_qty"], row2["receive_qty"], row2["confirm_qty"]) == (20, 0, 20)
    assert b2["void_count"] == 1  # 批次快照作废数

# 2. 作废使 open 批失效；结案写闸要求重跑；重跑后可结案
def test_void_invalidates_open_batch_and_blocks_seal(conn):
    b1 = svc.run_reconcile(conn, DAY)
    rid = _receive_id(conn, "RC-2002")
    voids.void_line(conn, "receive", rid, "张三", None)

    assert _batch(conn, b1["batch_id"])["status"] == "stale"  # 旧批失效但保留可查
    with pytest.raises(ValueError, match="须重新跑三联对账"):
        svc.seal_day(conn, DAY)

    b2 = svc.run_reconcile(conn, DAY)
    _dispose_all(conn, b2["batch_id"])
    sealed = svc.seal_day(conn, DAY)
    assert sealed["status"] == "sealed"
    assert _batch(conn, b1["batch_id"])["status"] == "stale"  # 历史批仍在

# 2b. 结案写闸：批次作废快照与当前不一致时禁止结案（防并发/防漏标）
def test_seal_gate_rejects_void_count_mismatch(conn):
    b1 = svc.run_reconcile(conn, DAY)
    rid = _receive_id(conn, "RC-2002")
    voids.void_line(conn, "receive", rid, "张三", None)
    # 模拟异常路径：旧批被错误地留在 open（绕过 stale 标记），写闸仍须拦截
    conn.execute("UPDATE reconcile_batches SET status='open' WHERE id=?", (b1["batch_id"],))
    conn.commit()
    with pytest.raises(ValueError, match="作废数"):
        svc.seal_day(conn, DAY)

# 3. 已封日两条路径均不可暗改：作废拒绝、写入拒绝、批次内容不变
def test_sealed_day_immutable(conn):
    b1 = svc.run_reconcile(conn, DAY)
    _dispose_all(conn, b1["batch_id"])
    svc.seal_day(conn, DAY)

    rid = _receive_id(conn, "RC-2002")
    with pytest.raises(ValueError, match="已结案"):
        voids.void_line(conn, "receive", rid, "张三", "事后改数")
    line = conn.execute("SELECT * FROM receive_lines WHERE id=?", (rid,)).fetchone()
    assert line["voided"] == 0 and line["qty"] == 20  # 未被暗改

    from app.services.gate import assert_writable
    with pytest.raises(ValueError, match="已结案"):
        assert_writable(conn, DAY)

    n = conn.execute("SELECT COUNT(*) c FROM match_rows WHERE batch_id=?", (b1["batch_id"],)).fetchone()["c"]
    assert n > 0  # 结案快照仍在

# 4. 审计能追到操作者与键
def test_audit_traces_operator_and_key(conn):
    rid = _receive_id(conn, "RC-2004")
    voids.void_line(conn, "receive", rid, "李四", "供应商取消送货")
    a = conn.execute("SELECT * FROM void_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert a["leg"] == "receive" and a["line_id"] == rid
    assert a["doc_no"] == "RC-2004"  # 业务键
    assert (a["supplier_code"], a["sku"], a["dock_day"], a["qty"]) == ("SUP-C", "SKU-巾-卷", DAY, 66)
    assert a["operator"] == "李四" and a["reason"] == "供应商取消送货"

# 5. 并发作废与结案：不得留下已封批含已废键的半截状态
def test_concurrent_void_and_seal_consistent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    seed_if_empty()
    # 造一个全对齐、可直接结案的到货日
    c = db.connect()
    c.execute("INSERT INTO expect_lines(doc_no, supplier_code, sku, dock_day, qty, note, created_at) VALUES ('EX-9','SUP-A','SKU-X','2026-11-19',5,NULL,'2026-11-19T08:00:00Z')")
    c.execute("INSERT INTO receive_lines(receive_no, supplier_code, sku, dock_day, qty, occurred_at, note, created_at) VALUES ('RC-9','SUP-A','SKU-X','2026-11-19',5,'2026-11-19T09:00:00Z',NULL,'2026-11-19T09:00:00Z')")
    c.execute("INSERT INTO confirm_lines(confirm_no, supplier_code, sku, dock_day, qty, note, created_at) VALUES ('CF-9','SUP-A','SKU-X','2026-11-19',5,NULL,'2026-11-19T10:00:00Z')")
    c.commit()
    svc.run_reconcile(c, "2026-11-19")
    c.close()

    barrier = threading.Barrier(2)
    results = {}

    def do_void():
        cc = db.connect()
        try:
            barrier.wait()
            rid = cc.execute("SELECT id FROM receive_lines WHERE receive_no='RC-9'").fetchone()["id"]
            voids.void_line(cc, "receive", rid, "王五", "并发作废")
            results["void"] = "ok"
        except ValueError as e:
            results["void"] = f"refused: {e}"
        finally:
            cc.close()

    def do_seal():
        cc = db.connect()
        try:
            barrier.wait()
            svc.seal_day(cc, "2026-11-19")
            results["seal"] = "ok"
        except ValueError as e:
            results["seal"] = f"refused: {e}"
        finally:
            cc.close()

    t1, t2 = threading.Thread(target=do_void), threading.Thread(target=do_seal)
    t1.start(); t2.start(); t1.join(); t2.join()

    chk = db.connect()
    line = chk.execute("SELECT voided FROM receive_lines WHERE receive_no='RC-9'").fetchone()
    batches = chk.execute("SELECT * FROM reconcile_batches WHERE dock_day='2026-11-19'").fetchall()
    sealed = [b for b in batches if b["status"] == "sealed"]
    if line["voided"] == 1:
        # 作废赢：不得存在已封批；open 批必须已失效
        assert sealed == [], "作废后不得结案"
        assert all(b["status"] != "open" for b in batches)
        assert results["seal"].startswith("refused")
    else:
        # 结案赢：作废必须被拒绝，已封批快照自洽
        assert results["void"].startswith("refused")
        assert len(sealed) == 1 and sealed[0]["void_count"] == 0
    chk.close()

# 6. 禁止静默 qty=0 冒充作废：无改 qty 路径，作废不动 qty，录入拒绝 qty<=0
def test_no_qty_zero_fake_void(conn):
    rid = _receive_id(conn, "RC-2001")
    voids.void_line(conn, "receive", rid, "张三", None)
    line = conn.execute("SELECT * FROM receive_lines WHERE id=?", (rid,)).fetchone()
    assert line["qty"] == 38  # 原值保留
    # 净值口径只认 voided 标志
    m = svc._sum_map(conn, "receive_lines", DAY)
    assert ("SUP-A", "SKU-米-25") not in m

def test_void_requires_operator_and_single_shot(conn):
    rid = _receive_id(conn, "RC-2002")
    with pytest.raises(ValueError, match="操作人"):
        voids.void_line(conn, "receive", rid, "  ", None)
    voids.void_line(conn, "receive", rid, "张三", None)
    with pytest.raises(ValueError, match="已作废"):
        voids.void_line(conn, "receive", rid, "张三", None)

def test_variance_fate_readonly_on_stale_batch(conn):
    b1 = svc.run_reconcile(conn, DAY)
    vid = conn.execute("SELECT id FROM variance_rows WHERE batch_id=? LIMIT 1", (b1["batch_id"],)).fetchone()["id"]
    rid = _receive_id(conn, "RC-2002")
    voids.void_line(conn, "receive", rid, "张三", None)
    with pytest.raises(ValueError, match="已失效"):
        svc.set_variance_fate(conn, vid, "accept_short")

# ---- HTTP 层：列表默认隐藏作废行、审计端点、qty=0 拒绝 ----

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    seed_if_empty()
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)

def test_http_list_hides_voided_by_default(client):
    rid = client.get("/api/receives").json()
    target = [r for r in rid if r["receive_no"] == "RC-2002"][0]
    res = client.post(f"/api/receives/{target['id']}/void", json={"operator": "张三", "reason": "测试"})
    assert res.status_code == 200

    visible = client.get("/api/receives").json()
    assert all(r["receive_no"] != "RC-2002" for r in visible)  # 默认隐藏

    expanded = client.get("/api/receives?include_voided=1").json()
    row = [r for r in expanded if r["receive_no"] == "RC-2002"][0]
    assert row["voided"] == 1 and row["qty"] == 20  # 可展开且原值在

    audit = client.get("/api/void-audit").json()
    assert audit[0]["doc_no"] == "RC-2002" and audit[0]["operator"] == "张三"

def test_http_void_sealed_day_rejected(client):
    client.post("/api/reconcile/run", json={"dock_day": DAY})
    batches = client.get(f"/api/reconcile/batches?dock_day={DAY}").json()
    for v in client.get(f"/api/reconcile/variances?batch_id={batches[0]['id']}").json():
        client.post(f"/api/reconcile/variances/{v['id']}/fate", json={"fate": "accept_short"})
    assert client.post("/api/reconcile/seal", json={"dock_day": DAY}).status_code == 200

    rid = [r for r in client.get("/api/receives").json() if r["receive_no"] == "RC-2002"][0]["id"]
    res = client.post(f"/api/receives/{rid}/void", json={"operator": "张三"})
    assert res.status_code == 400 and "已结案" in res.json()["detail"]

def test_http_reject_qty_zero(client):
    res = client.post("/api/expects", json={
        "doc_no": "EX-0", "supplier_code": "SUP-A", "sku": "SKU-X",
        "dock_day": DAY, "qty": 0,
    })
    assert res.status_code == 422  # qty<=0 直接拒绝，不存在 qty=0 冒充作废的入口
