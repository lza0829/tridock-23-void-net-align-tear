from __future__ import annotations

import sqlite3


def invalidate_open(conn: sqlite3.Connection, dock_day: str) -> int:
    """把当日所有 open 批置为 stale。

    记号驱动重算架构下，三腿任何数据变化（作废/新增正式腿）都会改变净值，
    按旧净值物化的 open 批即成为过期证据，必须强制失效、重跑后才能结案。
    须在已取得写锁的事务内调用，与扫描物化/结案串行化。
    """
    return conn.execute(
        "UPDATE reconcile_batches SET status='stale' WHERE dock_day=? AND status='open'",
        (dock_day,),
    ).rowcount
