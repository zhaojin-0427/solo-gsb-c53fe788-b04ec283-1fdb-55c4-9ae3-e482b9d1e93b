"""SQLite 连接与建表脚本。

报告表(reports)只有 INSERT，不提供任何 UPDATE/DELETE 路径，
配合应用层始终读取生成时写入的快照，保证旧报告不可变。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data"
DB_PATH = Path(os.environ.get("DATA_DIR", str(_DEFAULT_DATA))) / "adjudication.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    content     TEXT NOT NULL,            -- UTF-8 原文
    tokens      TEXT NOT NULL,            -- JSON: [[start,end],...]
    is_active   INTEGER NOT NULL DEFAULT 1,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(project_id, name)
);

-- 标签关系：kind='mutex' 互斥；kind='nested' 表示 outer 可包含 inner。
-- 未声明关系的不同标签默认互斥，同标签重叠恒为冲突。
CREATE TABLE IF NOT EXISTS label_relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    outer_id    INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    inner_id    INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('nested', 'mutex')),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotators (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    weight      INTEGER NOT NULL CHECK (weight BETWEEN 1 AND 10),
    created_at  TEXT NOT NULL,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS annotations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    annotator_id  INTEGER NOT NULL REFERENCES annotators(id) ON DELETE CASCADE,
    label_id      INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    start_offset  INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset    INTEGER NOT NULL CHECK (end_offset > start_offset),
    created_at    TEXT NOT NULL,
    UNIQUE(document_id, annotator_id, label_id, start_offset, end_offset)
);

-- 一次裁决（针对某份文档的一次求解+人工覆盖）。报告生成后冻结。
CREATE TABLE IF NOT EXISTS adjudications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    result_json TEXT NOT NULL,           -- 求解结果/覆盖后结果
    frozen      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 裁决者对单个候选 (start,end,label) 的强制采纳/驳回。
CREATE TABLE IF NOT EXISTS overrides (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    adjudication_id INTEGER NOT NULL REFERENCES adjudications(id) ON DELETE CASCADE,
    label_id         INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    start_offset     INTEGER NOT NULL,
    end_offset       INTEGER NOT NULL CHECK (end_offset > start_offset),
    action           TEXT NOT NULL CHECK (action IN ('force_include', 'force_exclude')),
    reason           TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    UNIQUE(adjudication_id, label_id, start_offset, end_offset)
);

-- 不可变报告：仅 INSERT。payload 即共识报告页面所展示的全部内容。
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    adjudication_id INTEGER NOT NULL REFERENCES adjudications(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    payload_json    TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(project_id, version)
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
