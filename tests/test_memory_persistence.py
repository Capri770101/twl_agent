"""存储回归：执行真实仓储 SQL；默认使用内存 SQL 测试替身。

设置 FLORA_TEST_DATABASE_URL 可在专用 PostgreSQL 的临时 schema 中验证，
不使用应用 DATABASE_URL，不访问生产业务表。
"""
import asyncio
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager

import pytest

from backend.storage import memory
from domain.requirements import FlowerRequirement, accumulate


@pytest.fixture
def store(monkeypatch):
    url = os.environ.get('FLORA_TEST_DATABASE_URL')
    if url:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from backend.storage.db import ConnectionAdapter, _SCHEMA

        raw = psycopg.connect(url, row_factory=dict_row, autocommit=True)
        schema = 'flora_test_' + uuid.uuid4().hex
        raw.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        conn = ConnectionAdapter(raw)
        for statement in _SCHEMA:
            conn.execute(statement)
    else:
        raw = sqlite3.connect(':memory:')
        raw.row_factory = sqlite3.Row
        conn = raw
        raw.executescript('''
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, requirement_json TEXT,
                preview TEXT, updated_at TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                ui TEXT, data TEXT, created_at TEXT
            );
        ''')

    @contextmanager
    def transaction():
        yield conn

    monkeypatch.setattr(memory, 'transaction', transaction)
    if url:
        conn.execute("INSERT INTO sessions(session_id, user_id, created_at, updated_at) VALUES ('s', 'u', NOW(), NOW())")
    else:
        conn.execute("INSERT INTO sessions(session_id) VALUES ('s')")
    try:
        yield conn
    finally:
        if url:
            raw.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        raw.close()


def test_requirement_survives_preview_and_multiple_turns(store):
    async def scenario():
        for part, preview in [
            (FlowerRequirement(recipient='妈妈'), '送妈妈'),
            (FlowerRequirement(occasion='生日'), '生日用'),
            (FlowerRequirement(budget_num=200), '预算200'),
            (FlowerRequirement(colors=['白', '绿']), '换成白绿色'),
            (FlowerRequirement(budget_num=150), '预算降到150'),
        ]:
            req = accumulate(await memory.get_requirement('s'), part)
            await memory.set_requirement('s', req)
            await memory.update_conversation_preview('s', preview)
        final = await memory.get_requirement('s')
        assert (final.recipient, final.occasion, final.budget_num, final.colors) == ('妈妈', '生日', 150, ['白', '绿'])
        assert store.execute("SELECT preview FROM sessions WHERE session_id='s'").fetchone()['preview'] == '预算降到150'
    asyncio.run(scenario())


@pytest.mark.parametrize('preview', ['普通预览', '[1,2]', 'null', '{"unrelated":true}'])
def test_plain_or_unrelated_legacy_preview_is_not_requirement(store, preview):
    asyncio.run(memory.update_conversation_preview('s', preview))
    assert asyncio.run(memory.get_requirement('s')) is None


def test_legacy_requirement_migrates_on_next_write(store):
    async def scenario():
        await memory.update_conversation_preview('s', json.dumps({'recipient': '妈妈', 'budget_num': 200}))
        old = await memory.get_requirement('s')
        assert old.recipient == '妈妈'
        await memory.set_requirement('s', accumulate(old, FlowerRequirement(budget_num=150)))
        await memory.update_conversation_preview('s', '新的展示摘要')
        assert (await memory.get_requirement('s')).budget_num == 150
    asyncio.run(scenario())


def test_history_uses_recent_visible_records_in_chronological_order(store):
    async def scenario():
        for i in range(15):
            await memory.save_messages('s', [
                {'role': 'user', 'content': f'需求{i}'},
                {'role': 'assistant', 'content': ''},
                {'role': 'tool', 'content': '内部结果'},
                {'role': 'assistant', 'content': f'方案{i}', 'ui': 'plan_card', 'data': {'plans': [{'name': f'方案{i}'}]}},
            ])
        await memory.save_messages('other', [{'role': 'user', 'content': '其他会话'}])
        history = await memory.load_history('s', 4)
        assert [m['content'] for m in history] == ['需求13', '方案13', '需求14', '方案14']
        assert history[-1]['data']['plans'][0]['name'] == '方案14'
        assert await memory.load_history('s', 0) == []
    asyncio.run(scenario())
