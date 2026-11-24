"""SQLite 存储层：表结构、事务和审计日志。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK (source IN ('meters', 'production', 'factors')),
    origin TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'complete', 'interrupted')),
    next_page INTEGER NOT NULL DEFAULT 1,
    total_pages INTEGER,
    cursor TEXT,
    config_json TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS batch_pages (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    page INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (batch_id, page)
);

CREATE TABLE IF NOT EXISTS meter_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    reading TEXT NOT NULL,
    meter TEXT NOT NULL,
    line TEXT NOT NULL,
    period TEXT NOT NULL,
    kwh REAL,
    version TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (meter, reading, version)
);

CREATE TABLE IF NOT EXISTS production_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    row_id TEXT NOT NULL,
    line TEXT NOT NULL,
    period TEXT NOT NULL,
    good_units REAL,
    scope TEXT,
    scope_raw TEXT,
    version TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- 显式来源版本必须唯一且不可被不同内容覆盖；未提供版本时允许按入库顺序追加修订。
CREATE UNIQUE INDEX IF NOT EXISTS ux_production_versioned
    ON production_observations (row_id, version) WHERE version <> '';

CREATE TABLE IF NOT EXISTS factor_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    factor TEXT NOT NULL,
    source TEXT NOT NULL,
    factor_key TEXT NOT NULL DEFAULT '',
    period TEXT NOT NULL,
    value REAL,
    revision INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (source, factor_key, period, revision)
);

CREATE TABLE IF NOT EXISTS meter_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meter TEXT NOT NULL,
    line TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'partial', 'pending')),
    replaces_meter TEXT,
    supersedes_id INTEGER REFERENCES meter_mappings(id),
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_scopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'pending')),
    scope TEXT NOT NULL,
    product TEXT,
    process_version TEXT,
    supersedes_id INTEGER REFERENCES production_scopes(id),
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    revision INTEGER NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    note TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES releases(id),
    UNIQUE (period, revision)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT,
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_meter_period ON meter_observations (period, line);
CREATE INDEX IF NOT EXISTS idx_meter_meter ON meter_observations (meter, period);
CREATE INDEX IF NOT EXISTS idx_production_period ON production_observations (period, line);
CREATE INDEX IF NOT EXISTS idx_factor_period ON factor_observations (period, source);
CREATE INDEX IF NOT EXISTS idx_mapping_meter ON meter_mappings (meter, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_scope_line ON production_scopes (line, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_release_period ON releases (period, revision);
"""


class Storage:
    """封装连接、事务和只追加审计日志。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.isolation_level = None
        self._savepoint = 0

    def close(self) -> None:
        self.conn.close()

    def initialize(self) -> None:
        # executescript 会先提交挂起事务，因此建表放在事务外，元数据写入再单独提交。
        self.conn.executescript(SCHEMA)
        with self.transaction():
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库结构版本 {row['value']} 与工具要求的 {SCHEMA_VERSION} 不一致"
                )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """可嵌套事务，内层使用 SAVEPOINT。"""
        if self.conn.in_transaction:
            self._savepoint += 1
            name = f"sp_{self._savepoint}"
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield
            except Exception:
                self.conn.execute(f"ROLLBACK TO {name}")
                self.conn.execute(f"RELEASE {name}")
                raise
            else:
                self.conn.execute(f"RELEASE {name}")
            finally:
                self._savepoint -= 1
        else:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def audit(
        self,
        *,
        at: str,
        actor: str,
        action: str,
        entity: str,
        entity_id: object | None,
        detail: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO audit (at, actor, action, entity, entity_id, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (at, actor, action, entity, None if entity_id is None else str(entity_id), detail),
        )

    def setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return default if row is None else row["value"]

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
