"""Additional memory writes keep their whole transaction off the request event loop."""
import asyncio
import threading
from contextlib import contextmanager

import pytest

from backend.storage import memory


@pytest.mark.parametrize('operation', [
    'get_or_create_session', 'save_messages', 'save_message', 'store_plan_version',
    'get_stage', 'get_requirement', 'get_long_term', 'search_user_history',
    'get_session_shop_id', 'get_session_context', 'update_stage', 'set_requirement',
    'set_long_term', 'get_session_json', 'get_session_flag', 'clear_session_flags',
    'get_user_memories', 'upsert_user_memory',
])
def test_remaining_writes_use_one_worker_for_entire_transaction(monkeypatch, operation):
    loop_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    seen = []

    class Cursor:
        def __init__(self, sql):
            self.sql = sql
        def fetchone(self):
            if 'FROM sessions' in self.sql:
                return {'session_id': 's', 'user_id': 'owner', 'stage': 'analyze',
                        'requirement_json': None, 'preview': None, 'shop_id': 'shop',
                        'entry': 'shop', 'product_id': None, 'product_title': None}
            if 'FROM memories' in self.sql:
                return {'value': '"saved"'}
            return None
        def fetchall(self):
            return []

    class Connection:
        def execute(self, sql, args):
            seen.append(('execute', threading.get_ident(), sql))
            return Cursor(sql)
        def executemany(self, sql, rows):
            seen.append(('executemany', threading.get_ident(), sql))

    @contextmanager
    def transaction():
        seen.append(('enter', threading.get_ident(), ''))
        entered.set()
        assert release.wait(2)
        try:
            yield Connection()
        finally:
            seen.append(('exit', threading.get_ident(), ''))

    monkeypatch.setattr(memory, 'transaction', transaction)

    async def run():
        if operation == 'get_or_create_session':
            call = memory.get_or_create_session('owner', 's')
        elif operation == 'save_messages':
            call = memory.save_messages('s', [{'role': 'user', 'content': 'hi'}])
        elif operation == 'save_message':
            call = memory.save_message('s', 'user', 'hi')
        elif operation == 'get_stage':
            call = memory.get_stage('s')
        elif operation == 'get_requirement':
            call = memory.get_requirement('s')
        elif operation == 'get_long_term':
            call = memory.get_long_term('owner')
        elif operation == 'search_user_history':
            call = memory.search_user_history('owner', '花', exclude_session='s')
        elif operation == 'get_session_shop_id':
            call = memory.get_session_shop_id('s')
        elif operation == 'get_session_context':
            call = memory.get_session_context('s')
        elif operation == 'update_stage':
            call = memory.update_stage('s', 'design')
        elif operation == 'set_requirement':
            from domain.requirements import FlowerRequirement
            call = memory.set_requirement('s', FlowerRequirement(budget_num=200))
        elif operation == 'set_long_term':
            call = memory.set_long_term('owner', 'favorite', 'rose')
        elif operation == 'get_session_json':
            call = memory.get_session_json('owner', 's', 'key')
        elif operation == 'get_session_flag':
            call = memory.get_session_flag('owner', 's', 'key')
        elif operation == 'clear_session_flags':
            call = memory.clear_session_flags('owner', 's', 'key')
        elif operation == 'get_user_memories':
            call = memory.get_user_memories('owner')
        elif operation == 'upsert_user_memory':
            call = memory.upsert_user_memory('owner', 's', 'key', 'value')
        else:
            call = memory.store_plan_version('owner', 's', {'plan_id': 'p'})
        task = asyncio.create_task(call)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 0.5)
        finally:
            release.set()
        return await asyncio.wait_for(task, 2)

    result = asyncio.run(run())
    assert seen[0][0] == 'enter' and seen[-1][0] == 'exit'
    assert all(t == seen[0][1] != loop_thread for _, t, _ in seen)
    if operation == 'store_plan_version':
        assert result == {'plan_id': 'p', 'version': 1}
        assert len([item for item in seen if item[0] == 'execute']) == 5
    if operation == 'get_or_create_session':
        assert result == 's'
