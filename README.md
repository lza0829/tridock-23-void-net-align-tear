# Tridock（到货三联台）

骨架：到货预报装载 → 实收入账 → 供应商回执装载 → 三方键对齐与差额处置 → 到货日结案封印。

- 前端 http://127.0.0.1:5900
- API http://127.0.0.1:10900

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10900 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5900
```

## 作废语义（全局唯一：软作废）

三腿装载单（预报/实收/回执）的作废**只**走软作废，全系统一种口径：

- **行标 `voided`，永不物理删除**，`qty` 保留原值；不提供任何改 `qty` 的入口，禁止用 `qty=0` 冒充作废。
- **净值口径**：三腿查询、对齐输入、结案对照一律按 `voided=0` 过滤；总览行数同口径，作废行数单列。
- **旧批失效**：作废使当日 `open` 对齐批置为 `stale`（保留可查，差额命运只读），须重跑三联对账。
- **批次快照**：每批记录 `void_count`（运行时当日作废行数）。
- **结案写闸**：只认 `open` 批，且批次 `void_count` 快照与当前一致才放行；作废与结案并发经 `BEGIN IMMEDIATE` 串行化，不会留下含已废键的已封批。
- **已封日**：拒绝作废（唯一规则，变更须走订正链路），也拒绝新增三腿行。
- **审计**：`void_audit` 记录腿别、行号、业务单号、供应商、货号、数量快照、操作人、原因、时间；`GET /api/void-audit` 可查，总览页展示。
- **列表**：默认隐藏作废行，`?include_voided=1` 或页面勾选「显示已作废」展开。

## 测试

```powershell
cd backend
.\.venv\Scripts\python -m pytest tests/ -q
```
