"""SQLite 连接、schema 初始化与事务管理。

并发策略：
  * WAL 日志 + busy_timeout，允许读写并发；
  * 所有写事务以 BEGIN IMMEDIATE 开启，写串行化；
  * (device_id, seq) 唯一约束兜底补传幂等/冲突判定；
  * 发布在同一个 IMMEDIATE 事务内取快照并落库，补传/修订/发布互不丢记录。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    rise_threshold TEXT NOT NULL,
    clear_threshold TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY,
    station_id  INTEGER NOT NULL REFERENCES stations(id),
    device_no   TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id                 INTEGER PRIMARY KEY,
    device_id          INTEGER NOT NULL REFERENCES devices(id),
    station_id         INTEGER NOT NULL REFERENCES stations(id),
    seq                INTEGER NOT NULL,
    observed_at        REAL NOT NULL,
    offset_minutes     INTEGER NOT NULL,
    water_level        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active',
    ingested_at        REAL NOT NULL,
    UNIQUE(device_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_readings_station_time
    ON readings(station_id, observed_at, id);

CREATE TABLE IF NOT EXISTS reading_revisions (
    id           INTEGER PRIMARY KEY,
    reading_id   INTEGER NOT NULL REFERENCES readings(id),
    action       TEXT NOT NULL,                 -- 'correct' | 'void'
    old_water_level TEXT,
    new_water_level TEXT,
    reason       TEXT NOT NULL,
    reviewer     TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_revisions_reading
    ON reading_revisions(reading_id, id);

CREATE TABLE IF NOT EXISTS publications (
    id          INTEGER PRIMARY KEY,
    station_id  INTEGER NOT NULL REFERENCES stations(id),
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,                  -- 冻结的时间线 JSON 快照
    created_at  REAL NOT NULL,
    publisher   TEXT NOT NULL,
    UNIQUE(station_id, version)
);

CREATE INDEX IF NOT EXISTS idx_publications_station
    ON publications(station_id, version);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_settings().db_path
    conn = sqlite3.connect(
        path,
        timeout=30.0,
        check_same_thread=False,
        isolation_level=None,  # 事务由本模块显式管理
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE 写事务：提交或回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def database_exists_with_data(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS c FROM stations").fetchone()
    return row is not None and row["c"] > 0
