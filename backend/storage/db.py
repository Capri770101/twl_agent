"""简化版 db.py —— 仅 SQLite，用于独立部署。"""
from __future__ import annotations
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from backend.config import settings

_thread_local = threading.local()

# ── Schema ──
# 纯智能体服务：不含用户管理（users/addresses/favorites/points/coupons），
# 这些由宿主平台（小程序/H5）自行管理，agent 只接收 user_id 字符串。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'analyze',
    title TEXT,
    preview TEXT,
    shop_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    ui TEXT,
    data TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_unique ON memories(user_id, category, key);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    desc TEXT,
    effect_image_url TEXT,
    merchant_name TEXT,
    tags TEXT,
    style TEXT,
    category_id TEXT,
    rating REAL NOT NULL DEFAULT 4.8,
    sold INTEGER NOT NULL DEFAULT 0,
    ai_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shops (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lat REAL,
    lng REAL,
    address TEXT,
    phone TEXT,
    hours TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    rating REAL NOT NULL DEFAULT 4.8,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shop_plans (
    shop_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'on',
    PRIMARY KEY (shop_id, plan_id)
);

CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    shop_id TEXT,
    sort INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diy_plans (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    name TEXT NOT NULL,
    requirement TEXT,
    recipient TEXT,
    occasion TEXT,
    style TEXT,
    budget REAL,
    color_scheme TEXT,
    flowers TEXT,
    packaging TEXT,
    meaning TEXT,
    diy_steps TEXT,
    care_tips TEXT,
    card_message TEXT,
    card_image_url TEXT,
    budget_breakdown TEXT,
    effect_image_url TEXT,
    difficulty TEXT,
    est_time INTEGER,
    shelf_life TEXT,
    suitable_for TEXT,
    caution TEXT,
    mood_tags TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed',
    order_count INTEGER NOT NULL DEFAULT 0,
    source_user_id TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    ref_type TEXT,
    ref_id TEXT,
    push_channel TEXT NOT NULL DEFAULT 'inbox',
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    shop_id TEXT NOT NULL,
    total REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'created',
    card_message TEXT,
    card_image_url TEXT,
    pay_jump TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    plan_id TEXT,
    name TEXT,
    price REAL NOT NULL DEFAULT 0,
    quantity INTEGER NOT NULL DEFAULT 1
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, category);
CREATE INDEX IF NOT EXISTS idx_plans_category ON plans(category_id);
"""


def get_conn() -> sqlite3.Connection:
    if not hasattr(_thread_local, 'conn'):
        Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA busy_timeout=30000;')
        _thread_local.conn = conn
    return _thread_local.conn


@contextmanager
def transaction():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    conn = get_conn()
    conn.executescript(_SCHEMA)
    conn.executescript(_INDEXES)
    conn.commit()
