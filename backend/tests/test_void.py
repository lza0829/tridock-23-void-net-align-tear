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


# ---- 夹具 1：页上已作废，扫描不得再按作废前量进匹配（记号驱动净值） ----

def test_fixture_voided_leg_never_enters_live_match(conn):
    b1 = svc.run_reconcile(conn, DAY)
    rid = _receive_id(conn, "RC-2002")  # SUP-A / SKU-油-5，实收 20
    voids.void_line(conn, "receive", rid, "张三", "重复录入")

    # 旧批已失效：它的对齐行是历史快照，可查但绝不允许拿去结案
    assert _batch(conn, b1["batch_id"])["status"] == "stale"
    with pytest.raises(ValueError):
        svc.seal_day(conn, DAY)

    # 重跑物化：扫描按 voided=0 净值，作废腿被摘掉，键仍在但实收净值为 0
    b2 = svc.run_reconcile(conn, DAY)
    row = conn.execute(
        "SELECT * FROM match_rows WHERE batch_id=? AND supplier_code='SUP-A' AND sku='SKU-油-5'",
        (b2["batch_id"],),
    ).fetchone()
    assert row["aligned"] == 0 and row["receive_qty"] == 0
    # 底层扫描口径本身不含作废行
    assert all(r["voided"] == 0 for r in conn.execute(
        "SELECT * FROM receive_lines WHERE dock_day=? AND voided=0", (DAY,)).fetchall())
    net = svc._sum_map(conn, "receive_lines", DAY)
    assert net.get(("SUP-A", "SKU-油-5"), 0.0) == 0.0


def test_void_all_lines_of_leg_drops_key_from_scan_closure(conn):
    # 某键在某腿只有一行，作废后该键从该腿扫描闭包彻底消失，绝不留幽灵量
    b = svc.run_reconcile(conn, DAY)
    rid = _receive_id(conn, "RC-2003")  # SUP-B / SKU-箱-大，唯一实收行
    voids.void_line(conn, "receive", rid, "张三", None)
    b2 = svc.run_reconcile(conn, DAY)
    row = conn.execute(
        "SELECT * FROM match_rows WHERE batch_id=? AND supplier_code='SUP-B' AND sku='SKU-箱-大'",
        (b2["batch_id"],),
    ).fetchone()
    assert row is not None and row["receive_qty"] == 0  # 键由其他腿带入，实收净值为 0
    assert ("SUP-B", "SKU-箱-大") not in svc._sum_map(conn, "receive_lines", DAY)


# ---- 夹具 2：行删了痕迹空、批明细挂幽灵键——软作废无物理删除，痕迹与行都在 ----

def test_fixture_no_physical_delete_and_no_ghost_key(conn):
    before = conn.execute("SELECT COUNT(*) c FROM receive_lines").fetchone()["c"]
    rid = _receive_id(conn, "RC-2004")
    voids.void_line(conn, "receive", rid, "李四", "供应商取消")
    after = conn.execute("SELECT COUNT(*) c FROM receive_lines").fetchone()["c"]
    assert before == after  # 行仍在正式表，没有物理删除入口

    # 痕迹固化：行原值 + 作废前后该腿净值，追得到前后量
    line = conn.execute("SELECT * FROM receive_lines WHERE id=?", (rid,)).fetchone()
    assert line["voided"] == 1 and line["qty"] == 66
    audit = conn.execute("SELECT * FROM void_audit WHERE line_id=? AND leg='receive'", (rid,)).fetchone()
    assert audit["qty"] == 66
    assert audit["net_before"] - audit["net_after"] == 66  # 前后量差额恰为作废行原值

    # 现行 open 批不挂幽灵键：重跑后所有现行批明细均能对应到未作废净值
    b = svc.run_reconcile(conn, DAY)
    live = conn.execute(
        "SELECT receive_qty FROM match_rows WHERE batch_id=?", (b["batch_id"],)
    ).fetchall()
    # SUP-C/SKU-巾-卷 的实收净值须为 0（66 已作废），而不是仍挂 66
    ghost = conn.execute(
        "SELECT * FROM match_rows WHERE batch_id=? AND supplier_code='SUP-C' AND sku='SKU-巾-卷'",
        (b["batch_id"],),
    ).fetchone()
    assert ghost["receive_qty"] == 0
    assert all(r["receive_qty"] >= 0 for r in live)


def test_no_physical_delete_endpoint_or_sql():
    # 代码层面不存在三腿行的物理删除路径（仅 open 批重跑时替换自己的物化行）
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "app"
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8").upper()
        for table in ("EXPECT_LINES", "RECEIVE_LINES", "CONFIRM_LINES"):
            assert f"DELETE FROM {table}" not in text, f"{py.name} 不得物理删除 {table}"


# ---- 夹具 3：作废回执成功后，旧对账批没失效仍拿过期证据去封 ----

def test_fixture_stale_batch_cannot_be_sealed_even_if_fates_cleared(conn):
    b1 = svc.run_reconcile(conn, DAY)
    _dispose_all(conn, b1["batch_id"])  # 差额已清零，看似可封
    cid = conn.execute(
        "SELECT id FROM confirm_lines WHERE confirm_no='CF-3002'").fetchone()["id"]
    voids.void_line(conn, "confirm", cid, "张三", "回执作废")

    assert _batch(conn, b1["batch_id"])["status"] == "stale"
    # 即使差额命运都处置过，stale 批也不是可封对象
    with pytest.raises(ValueError, match="重新跑三联对账"):
        svc.seal_day(conn, DAY)

    # 写闸双保险：人为把 stale 拨回 open，作废数快照不一致仍拦截
    conn.execute("UPDATE reconcile_batches SET status='open' WHERE id=?", (b1["batch_id"],))
    conn.commit()
    with pytest.raises(ValueError, match="作废数"):
        svc.seal_day(conn, DAY)


def test_http_fixture_stale_evidence_rejected(client):
    client.post("/api/reconcile/run", json={"dock_day": DAY})
    rid = [r for r in client.get("/api/receives").json() if r["receive_no"] == "RC-2002"][0]["id"]
    assert client.post(f"/api/receives/{rid}/void", json={"operator": "张三"}).status_code == 200
    # 旧批已 stale，结案必须 400
    res = client.post("/api/reconcile/seal", json={"dock_day": DAY})
    assert res.status_code == 400 and "重新跑三联对账" in res.json()["detail"]
    batches = client.get(f"/api/reconcile/batches?dock_day={DAY}").json()
    assert batches[0]["status"] == "stale"  # 历史证据保留可查，不是消失


# ---- 夹具 4：作废与扫描抢跑，半截量不得钻进 sealed（重复并发压测） ----

@pytest.mark.parametrize("round_", range(8))
def test_fixture_concurrent_void_vs_seal_no_half_sealed(tmp_path, monkeypatch, round_):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / f"t{round_}.db")
    seed_if_empty()
    c = db.connect()
    c.execute("INSERT INTO expect_lines(doc_no, supplier_code, sku, dock_day, qty, note, created_at) VALUES ('EX-9','SUP-A','SKU-X','2026-11-19',5,NULL,'2026-11-19T08:00:00Z')")
    c.execute("INSERT INTO receive_lines(receive_no, supplier_code, sku, dock_day, qty, occurred_at, note, created_at) VALUES ('RC-9','SUP-A','SKU-X','2026-11-19',5,'2026-11-19T09:00:00Z',NULL,'2026-11-19T09:00:00Z')")
    c.execute("INSERT INTO confirm_lines(confirm_no, supplier_code, sku, dock_day, qty, note, created_at) VALUES ('CF-9','SUP-A','SKU-X','2026-11-19',5,NULL,'2026-11-19T10:00:00Z')")
    c.commit()
    svc.run_reconcile(c, "2026-11-19")
    c.close()

    barrier = threading.Barrier(2)
    outcomes = {}

    def do_void():
        cc = db.connect()
        try:
            barrier.wait()
            rid = cc.execute("SELECT id FROM receive_lines WHERE receive_no='RC-9'").fetchone()["id"]
            voids.void_line(cc, "receive", rid, "王五", "抢跑作废")
            outcomes["void"] = "ok"
        except ValueError as e:
            outcomes["void"] = f"refused: {e}"
        finally:
            cc.close()

    def do_seal():
        cc = db.connect()
        try:
            barrier.wait()
            svc.seal_day(cc, "2026-11-19")
            outcomes["seal"] = "ok"
        except ValueError as e:
            outcomes["seal"] = f"refused: {e}"
        finally:
            cc.close()

    t1, t2 = threading.Thread(target=do_void), threading.Thread(target=do_seal)
    t1.start(); t2.start(); t1.join(); t2.join()

    chk = db.connect()
    line = chk.execute("SELECT voided FROM receive_lines WHERE receive_no='RC-9'").fetchone()
    sealed = chk.execute(
        "SELECT * FROM reconcile_batches WHERE dock_day='2026-11-19' AND status='sealed'").fetchall()
    if line["voided"] == 1:
        # 作废赢：不存在任何 sealed 批，且无 open 批漏网
        assert outcomes["seal"].startswith("refused")
        assert sealed == []
        assert chk.execute(
            "SELECT COUNT(*) c FROM reconcile_batches WHERE dock_day='2026-11-19' AND status='open'"
        ).fetchone()["c"] == 0
    else:
        # 结案赢：作废被拒，sealed 快照自洽且不含已废量
        assert outcomes["void"].startswith("refused")
        assert len(sealed) == 1 and sealed[0]["void_count"] == 0
        mr = chk.execute(
            "SELECT receive_qty FROM match_rows WHERE batch_id=? AND sku='SKU-X'",
            (sealed[0]["id"],)).fetchone()
        assert mr["receive_qty"] == 5
    chk.close()


# ---- 正式腿写入与批失效/写闸联动（扫描物化、写闸、看板同源） ----

def test_insert_line_invalidates_open_batch_and_sealed_rejects_insert(conn):
    b1 = svc.run_reconcile(conn, DAY)
    # 新增一条实收腿：净值变化，旧 open 批必须同步失效，不能等作废才失效
    conn.execute("BEGIN IMMEDIATE")
    from app.services.batches import invalidate_open
    from app.services.gate import assert_writable
    assert_writable(conn, DAY)
    conn.execute(
        "INSERT INTO receive_lines(receive_no, supplier_code, sku, dock_day, qty, occurred_at, note, created_at) "
        "VALUES ('RC-2099','SUP-A','SKU-油-5',?,7,'2026-11-18T13:00:00Z',NULL,'2026-11-18T13:00:00Z')",
        (DAY,))
    invalidate_open(conn, DAY)
    conn.commit()
    assert _batch(conn, b1["batch_id"])["status"] == "stale"
    with pytest.raises(ValueError, match="重新跑三联对账"):
        svc.seal_day(conn, DAY)
    b2 = svc.run_reconcile(conn, DAY)
    row = conn.execute(
        "SELECT * FROM match_rows WHERE batch_id=? AND supplier_code='SUP-A' AND sku='SKU-油-5'",
        (b2["batch_id"],)).fetchone()
    assert row["receive_qty"] == 27  # 20 + 7 净值物化

    # 已封日正式腿写入也被写闸拒绝
    _dispose_all(conn, b2["batch_id"])
    svc.seal_day(conn, DAY)
    from app.services.gate import assert_writable as aw
    with pytest.raises(ValueError, match="已结案"):
        aw(conn, DAY)


def test_http_insert_after_void_then_rerun_counts_are_consistent(client):
    # 端到端：作废 → 看板/列表/重跑对账三处口径一致，不允许按钮灰了数还撒谎
    rid = [r for r in client.get("/api/receives").json() if r["receive_no"] == "RC-2002"][0]["id"]
    client.post(f"/api/receives/{rid}/void", json={"operator": "张三", "reason": "重复"})

    dash = client.get("/api/dashboard").json()
    assert dash["voided_count"] == 1
    assert all(r["receive_no"] != "RC-2002" for r in client.get("/api/receives").json())

    run = client.post("/api/reconcile/run", json={"dock_day": DAY}).json()
    assert run["void_count"] == 1
    row = [m for m in client.get(f"/api/reconcile/matches?batch_id={run['batch_id']}").json()
           if m["sku"] == "SKU-油-5"][0]
    assert row["receive_qty"] == 0 and row["aligned"] == 0
    # 审计带前后净值
    audit = client.get("/api/void-audit").json()[0]
    assert audit["net_before"] > audit["net_after"] and audit["qty"] == 20


def test_http_insert_invalidates_open_batch_and_sealed_blocks_writes(client):
    client.post("/api/reconcile/run", json={"dock_day": DAY})
    assert client.get(f"/api/reconcile/batches?dock_day={DAY}").json()[0]["status"] == "open"
    # 新增正式腿改变净值，open 批须联动失效（三腿写入端点同构，实收为代表）
    res = client.post("/api/receives", json={
        "receive_no": "RC-2099", "supplier_code": "SUP-A", "sku": "SKU-油-5",
        "dock_day": DAY, "qty": 7,
    })
    assert res.status_code == 200 and res.json()["stale_batches"] == 1
    assert client.get(f"/api/reconcile/batches?dock_day={DAY}").json()[0]["status"] == "stale"
    assert client.post("/api/reconcile/seal", json={"dock_day": DAY}).status_code == 400

    # 重跑 → 处置差额 → 结案；结案后新增腿与作废双双被写闸拒绝
    b = client.post("/api/reconcile/run", json={"dock_day": DAY}).json()
    for v in client.get(f"/api/reconcile/variances?batch_id={b['batch_id']}").json():
        client.post(f"/api/reconcile/variances/{v['id']}/fate", json={"fate": "accept_short"})
    assert client.post("/api/reconcile/seal", json={"dock_day": DAY}).status_code == 200

    blocked = client.post("/api/receives", json={
        "receive_no": "RC-2100", "supplier_code": "SUP-A", "sku": "SKU-X",
        "dock_day": DAY, "qty": 1,
    })
    assert blocked.status_code == 400 and "已结案" in blocked.json()["detail"]
    rid = [r for r in client.get("/api/receives").json() if r["receive_no"] == "RC-2001"][0]["id"]
    blocked_void = client.post(f"/api/receives/{rid}/void", json={"operator": "张三"})
    assert blocked_void.status_code == 400 and "已结案" in blocked_void.json()["detail"]


