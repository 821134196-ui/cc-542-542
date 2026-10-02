"""SQLite 数据层：影厅 / 放映机 / 灯泡 / 场次 / 换灯工单 / 试映 / 审批 / 异常。

设备状态全部落在 SQLite；亮度照片文件在本地 data/photos（见 photos.py）。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    role  TEXT NOT NULL CHECK (role IN ('technician', 'supervisor'))
);

CREATE TABLE IF NOT EXISTS halls (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projectors (
    id         INTEGER PRIMARY KEY,
    hall_id    INTEGER NOT NULL REFERENCES halls(id),
    model      TEXT NOT NULL,          -- 放映机型号
    lamp_model TEXT NOT NULL,          -- 该机型要求的灯泡备件型号
    status     TEXT NOT NULL DEFAULT 'idle'
               CHECK (status IN ('idle', 'running', 'maintenance', 'down'))
);

CREATE TABLE IF NOT EXISTS lamps (
    id               INTEGER PRIMARY KEY,
    serial           TEXT NOT NULL UNIQUE,   -- 灯泡序列号
    model            TEXT NOT NULL,          -- 灯泡型号（备件匹配依据）
    threshold_hours  REAL NOT NULL,          -- 厂家规定的更换阈值（小时）
    cumulative_hours REAL NOT NULL DEFAULT 0,-- 累计点亮小时
    status           TEXT NOT NULL DEFAULT 'spare'
               -- in_use 在用 / spare 备件 / removed 已拆下 / failed 异常失效 / scrapped 报废
               CHECK (status IN ('in_use', 'spare', 'removed', 'failed', 'scrapped'))
);

CREATE TABLE IF NOT EXISTS installations (
    id            INTEGER PRIMARY KEY,
    projector_id  INTEGER NOT NULL REFERENCES projectors(id),
    lamp_id       INTEGER NOT NULL REFERENCES lamps(id),
    work_order_id INTEGER,               -- 由哪张工单装上/拆下
    installed_at  TEXT NOT NULL,
    removed_at    TEXT
);

CREATE TABLE IF NOT EXISTS shows (
    id           INTEGER PRIMARY KEY,
    hall_id      INTEGER NOT NULL REFERENCES halls(id),
    title        TEXT NOT NULL,
    start_time   TEXT NOT NULL,          -- ISO 本地时间
    duration_min INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'scheduled'
                 CHECK (status IN ('scheduled', 'delayed', 'completed', 'cancelled')),
    delay_min    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS work_orders (
    id            INTEGER PRIMARY KEY,
    projector_id  INTEGER NOT NULL REFERENCES projectors(id),
    technician_id INTEGER NOT NULL REFERENCES users(id),   -- 换灯人
    reason        TEXT,
    status        TEXT NOT NULL DEFAULT 'in_progress'
                  CHECK (status IN ('in_progress', 'pending_release', 'released', 'cancelled')),
    old_lamp_id   INTEGER REFERENCES lamps(id),
    new_lamp_id   INTEGER REFERENCES lamps(id),
    brightness_before REAL,              -- 换灯前实测亮度（试映前后成对展示用）
    cancel_reason TEXT,
    created_at    TEXT NOT NULL,
    closed_at     TEXT
);

-- 换灯步骤固定编号 1..5，避免遗漏：1 断电 2 冷却 3 安装 4 对焦 5 试映
CREATE TABLE IF NOT EXISTS wo_steps (
    work_order_id INTEGER NOT NULL REFERENCES work_orders(id),
    step_no       INTEGER NOT NULL CHECK (step_no BETWEEN 1 AND 5),
    name          TEXT NOT NULL,
    done          INTEGER NOT NULL DEFAULT 0,
    done_at       TEXT,
    note          TEXT,
    PRIMARY KEY (work_order_id, step_no)
);

CREATE TABLE IF NOT EXISTS screenings (
    id                INTEGER PRIMARY KEY,
    work_order_id     INTEGER NOT NULL REFERENCES work_orders(id),
    brightness_before REAL NOT NULL,     -- 试映前亮度 fL
    brightness_after  REAL NOT NULL,     -- 试映后亮度 fL
    photo_before      TEXT,              -- 本地照片路径
    photo_after       TEXT,
    result            TEXT NOT NULL CHECK (result IN ('pass', 'insufficient')),
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY,
    work_order_id INTEGER NOT NULL REFERENCES work_orders(id),
    approver_id   INTEGER NOT NULL REFERENCES users(id),
    decision      TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    note          TEXT,
    created_at    TEXT NOT NULL
);

-- 超阈值设备临时使用：必须由放映主管批准
CREATE TABLE IF NOT EXISTS overrides (
    id            INTEGER PRIMARY KEY,
    projector_id  INTEGER NOT NULL REFERENCES projectors(id),
    supervisor_id INTEGER NOT NULL REFERENCES users(id),
    reason        TEXT,
    expires_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- 维修窗口互斥：同一放映机同一时间只允许一名技术员占用
CREATE TABLE IF NOT EXISTS maintenance_windows (
    id            INTEGER PRIMARY KEY,
    projector_id  INTEGER NOT NULL REFERENCES projectors(id),
    technician_id INTEGER NOT NULL REFERENCES users(id),
    opened_at     TEXT NOT NULL,
    closed_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mw_open
    ON maintenance_windows(projector_id) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY,
    type          TEXT NOT NULL CHECK (type IN (
                      'lamp_failure',          -- 异常熄灯
                      'timer_reset_failed',    -- 计时器重置失败
                      'spare_mismatch',        -- 备件型号不符
                      'cancelled_midway',      -- 换灯中途取消
                      'insufficient_brightness', -- 试映亮度不足
                      'show_delay')),          -- 场次延误
    projector_id  INTEGER REFERENCES projectors(id),
    work_order_id INTEGER REFERENCES work_orders(id),
    show_id       INTEGER REFERENCES shows(id),
    detail        TEXT,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolution    TEXT,
    created_at    TEXT NOT NULL,
    resolved_at   TEXT
);

CREATE TABLE IF NOT EXISTS show_impacts (
    id         INTEGER PRIMARY KEY,
    show_id    INTEGER NOT NULL REFERENCES shows(id),
    cause      TEXT NOT NULL,              -- lamp_failure / maintenance / show_delay ...
    ref_id     INTEGER,                    -- 关联异常或工单
    impact     TEXT NOT NULL CHECK (impact IN ('at_risk', 'delayed', 'cancelled')),
    delay_min  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

STEP_NAMES = {1: "断电", 2: "冷却", 3: "安装", 4: "对焦", 5: "试映"}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    # check_same_thread=False：服务层用锁串行化访问（见 server.App.lock）
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def is_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM halls").fetchone()
    return row["n"] == 0


def rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params=()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None
