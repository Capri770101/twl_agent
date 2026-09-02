"""生产数据库访问层：PostgreSQL-only。

本服务不再使用 SQLite。所有业务存储和平台数据访问必须通过
DATABASE_URL 连接到 PostgreSQL（或兼容 PostgreSQL 协议的服务）。
连接按线程复用，事务由 context manager 统一提交/回滚；`?` 占位符由
兼容包装器转换为 psycopg 使用的 `%s`，以保持现有仓储代码可迁移。
"""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from backend.config import settings

_thread_local = threading.local()

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'analyze', title TEXT, preview TEXT, shop_id TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS messages (id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, ui TEXT, data TEXT, created_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memories (id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, category TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_unique ON memories(user_id, category, key)""",
    """CREATE TABLE IF NOT EXISTS user_preferences (user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (user_id, key))""",
    """CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, name TEXT NOT NULL, price DOUBLE PRECISION NOT NULL DEFAULT 0, desc TEXT, effect_image_url TEXT, merchant_name TEXT, tags TEXT, style TEXT, category_id TEXT, rating DOUBLE PRECISION NOT NULL DEFAULT 4.8, sold INTEGER NOT NULL DEFAULT 0, ai_reason TEXT, created_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS shops (id TEXT PRIMARY KEY, name TEXT NOT NULL, lat DOUBLE PRECISION, lng DOUBLE PRECISION, address TEXT, phone TEXT, hours TEXT, status TEXT NOT NULL DEFAULT 'active', rating DOUBLE PRECISION NOT NULL DEFAULT 4.8, created_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS shop_plans (shop_id TEXT NOT NULL, plan_id TEXT NOT NULL, stock INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'on', PRIMARY KEY (shop_id, plan_id))""",
    """CREATE TABLE IF NOT EXISTS categories (id TEXT PRIMARY KEY, name TEXT NOT NULL, shop_id TEXT, sort INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS operations_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS diy_plans (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, fingerprint TEXT NOT NULL, name TEXT NOT NULL, requirement TEXT, recipient TEXT, occasion TEXT, style TEXT, budget DOUBLE PRECISION, color_scheme TEXT, flowers TEXT, packaging TEXT, meaning TEXT, diy_steps TEXT, care_tips TEXT, card_message TEXT, card_image_url TEXT, budget_breakdown TEXT, effect_image_url TEXT, difficulty TEXT, est_time INTEGER, shelf_life TEXT, suitable_for TEXT, caution TEXT, mood_tags TEXT, status TEXT NOT NULL DEFAULT 'confirmed', order_count INTEGER NOT NULL DEFAULT 0, source_user_id TEXT, created_at TIMESTAMPTZ NOT NULL, confirmed_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, type TEXT NOT NULL, title TEXT NOT NULL, body TEXT, ref_type TEXT, ref_id TEXT, push_channel TEXT NOT NULL DEFAULT 'inbox', is_read INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, shop_id TEXT NOT NULL, total DOUBLE PRECISION NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'created', card_message TEXT, card_image_url TEXT, pay_jump TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS order_items (id BIGSERIAL PRIMARY KEY, order_id TEXT NOT NULL, plan_id TEXT, name TEXT, price DOUBLE PRECISION NOT NULL DEFAULT 0, quantity INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS image_tasks (task_id TEXT PRIMARY KEY, user_id TEXT, status TEXT NOT NULL, prompt TEXT NOT NULL, result_url TEXT, error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """ALTER TABLE image_tasks ADD COLUMN IF NOT EXISTS user_id TEXT""",
    """CREATE TABLE IF NOT EXISTS mapping_drafts (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, schema_name TEXT NOT NULL, schema_fingerprint TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'draft', draft_json JSONB NOT NULL, created_by TEXT NOT NULL DEFAULT 'agent', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), reviewed_by TEXT, reviewed_at TIMESTAMPTZ)""",
    """CREATE INDEX IF NOT EXISTS idx_mapping_drafts_source ON mapping_drafts(source_id, schema_fingerprint, version DESC)""",
    """CREATE TABLE IF NOT EXISTS mapping_audit (id BIGSERIAL PRIMARY KEY, source_id TEXT NOT NULL, mapping_id TEXT, action TEXT NOT NULL, actor TEXT NOT NULL, details JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
]

_INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC)',
    'CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id ASC)',
    'CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, category)',
    'CREATE INDEX IF NOT EXISTS idx_plans_category ON plans(category_id)',
    'CREATE INDEX IF NOT EXISTS idx_image_tasks_updated ON image_tasks(updated_at DESC)',
    'CREATE INDEX IF NOT EXISTS idx_image_tasks_user ON image_tasks(user_id, updated_at DESC)',
]


def _database_url() -> str:
    url = (settings.DATABASE_URL or '').strip()
    if not url or url.lower().startswith('sqlite'):
        raise RuntimeError('DATABASE_URL 必须配置为 PostgreSQL；生产环境禁止 SQLite')
    if not url.lower().startswith(('postgresql://', 'postgres://')):
        raise RuntimeError('仅支持 PostgreSQL DATABASE_URL')
    return url


def _convert_placeholders(sql: str) -> str:
    return re.sub(r'(?<!%)\?', '%s', sql)


class ConnectionAdapter:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    @property
    def closed(self) -> bool:
        return bool(self._conn.closed)

    def execute(self, sql: str, params: Any = None):
        return self._conn.execute(_convert_placeholders(sql), params or ())

    def executemany(self, sql: str, params):
        return self._conn.executemany(_convert_placeholders(sql), params)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


def get_conn() -> ConnectionAdapter:
    conn = getattr(_thread_local, 'conn', None)
    if conn is None or conn.closed:
        raw = psycopg.connect(_database_url(), row_factory=dict_row, connect_timeout=10)
        _thread_local.conn = ConnectionAdapter(raw)
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


def init_db() -> None:
    """初始化本服务所需的 PostgreSQL 表和索引。"""
    with transaction() as conn:
        for statement in (*_SCHEMA, *_INDEXES):
            conn.execute(statement)
