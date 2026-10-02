"""SQLite 存储：设备状态、工单、试映、事故、审批、维修锁。"""
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS halls (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS projectors (
    id INTEGER PRIMARY KEY,
    hall_id INTEGER NOT NULL UNIQUE REFERENCES halls(id),
    model TEXT NOT NULL,
    required_lamp_model TEXT NOT NULL,
    required_lumens REAL NOT NULL DEFAULT 8000
);

CREATE TABLE IF NOT EXISTS lamps (
    id INTEGER PRIMARY KEY,
    projector_id INTEGER REFERENCES projectors(id),
    serial_no TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL,
    lit_hours REAL NOT NULL DEFAULT 0,
    threshold_hours REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('installed','removed')),
    installed_at TEXT,
    removed_at TEXT
);

CREATE TABLE IF NOT EXISTS spares (
    id INTEGER PRIMARY KEY,
    serial_no TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL,
    threshold_hours REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY,
    hall_id INTEGER NOT NULL REFERENCES halls(id),
    title TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    duration_min REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK(status IN ('scheduled','delayed','cancelled','screened')),
    delay_min REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    projector_id INTEGER NOT NULL REFERENCES projectors(id),
    hall_id INTEGER NOT NULL REFERENCES halls(id),
    technician TEXT NOT NULL,
    spare_id INTEGER REFERENCES spares(id),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','in_progress','completed','cancelled')),
    new_lamp_id INTEGER REFERENCES lamps(id),
    before_lumens REAL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cancelled_at TEXT,
    cancel_note TEXT
);

CREATE TABLE IF NOT EXISTS ticket_steps (
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    step_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','done')),
    done_at TEXT,
    done_by TEXT,
    note TEXT,
    PRIMARY KEY (ticket_id, step_no)
);

CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    before_lumens REAL NOT NULL,
    after_lumens REAL NOT NULL,
    required_lumens REAL NOT NULL,
    passed INTEGER NOT NULL,
    before_photo TEXT,
    after_photo TEXT,
    tested_by TEXT NOT NULL,
    tested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    hall_id INTEGER REFERENCES halls(id),
    projector_id INTEGER REFERENCES projectors(id),
    ticket_id INTEGER REFERENCES tickets(id),
    show_id INTEGER REFERENCES shows(id),
    detail TEXT NOT NULL,
    resolution TEXT,
    handled_by TEXT,
    handled_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    projector_id INTEGER NOT NULL REFERENCES projectors(id),
    approver TEXT NOT NULL,
    approver_role TEXT NOT NULL,
    reason TEXT NOT NULL,
    ticket_id INTEGER REFERENCES tickets(id),
    active INTEGER NOT NULL DEFAULT 1,
    approved_at TEXT NOT NULL,
    usable_until TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL UNIQUE REFERENCES tickets(id),
    signer TEXT NOT NULL,
    signer_role TEXT NOT NULL,
    note TEXT,
    signed_at TEXT NOT NULL
);

-- 维修窗口互斥：同一放映机同时只允许一张占用中的工单
CREATE TABLE IF NOT EXISTS maintenance_locks (
    projector_id INTEGER PRIMARY KEY REFERENCES projectors(id),
    ticket_id INTEGER NOT NULL,
    technician TEXT NOT NULL,
    since TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lamps_projector ON lamps(projector_id);
CREATE INDEX IF NOT EXISTS idx_shows_hall ON shows(hall_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_incidents_projector ON incidents(projector_id);
"""


def connect(db_path=None):
    conn = sqlite3.connect(db_path or config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path=None):
    config.ensure_dirs()
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def tx(conn):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
