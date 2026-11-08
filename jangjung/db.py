"""규칙·포트폴리오·주문·확인대기 저장용 SQLite."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    side TEXT NOT NULL,              -- buy | sell
    condition TEXT NOT NULL,         -- price_at_or_above | price_at_or_below | return_at_or_above | return_at_or_below
    threshold REAL NOT NULL,
    qty_mode TEXT NOT NULL,          -- shares | fraction | amount
    qty_value REAL NOT NULL,
    max_triggers INTEGER NOT NULL DEFAULT 1,
    triggered INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',   -- active | paused | waiting_confirm | done | cancelled
    source_text TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',     -- open | done | expired
    result TEXT,
    message_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS autoinvest (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tier INTEGER NOT NULL,
    principal REAL NOT NULL,
    cash REAL NOT NULL,              -- 자동투자 몫으로 배정된 가상 현금
    targets TEXT NOT NULL,           -- {"069500": 40, ...}
    status TEXT NOT NULL,            -- building | active | halted_loss | stopped
    tranches_total INTEGER NOT NULL,
    tranches_done INTEGER NOT NULL DEFAULT 0,
    last_build_date TEXT,
    last_rebalance_month TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    bucket TEXT NOT NULL,            -- auto (자동투자 장부)
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    PRIMARY KEY (bucket, symbol)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    detected_at TEXT,
    source TEXT NOT NULL,            -- rule | auto_build | rebalance
    ref_id TEXT,
    bucket TEXT NOT NULL,            -- rule | auto
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    decision_price REAL NOT NULL,
    order_price REAL,
    broker_order_no TEXT,
    status TEXT NOT NULL,            -- submitting | submitted | partial | filled | failed | rejected
    filled_qty INTEGER NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    ledger_qty INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    evidence TEXT,
    error TEXT,
    journaled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,              -- bot | user
    via TEXT,                        -- telegram | web | console
    text TEXT NOT NULL,
    buttons TEXT,                    -- 확인 카드 버튼 JSON
    resolved TEXT                    -- 카드 처리 결과 (버튼 비활성)
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    date TEXT PRIMARY KEY,
    auto_value REAL,
    auto_principal REAL,
    rule_fills INTEGER,
    active_rules INTEGER
);
"""


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.q("PRAGMA table_info(daily_snapshots)")}
        if "detail" not in cols:          # 보고서용 장 마감 보유·비중 스냅샷 (JSON)
            self.conn.execute("ALTER TABLE daily_snapshots ADD COLUMN detail TEXT")

    def q(self, sql: str, args: tuple | list = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple | list = ()) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def x(self, sql: str, args: tuple | list = ()) -> int:
        cur = self.conn.execute(sql, args)
        return cur.lastrowid

    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False)))

    def kv_del(self, key: str) -> None:
        self.x("DELETE FROM kv WHERE key=?", (key,))
