"""Merchant ticket reads must leave the event loop free and keep transactions on one worker."""
import asyncio
import threading
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from backend.routers import merchant_tickets


@pytest.mark.parametrize('detail', [False, True])
def test_ticket_read_transaction_runs_on_worker(monkeypatch, detail):
    loop_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    observed = []

    class Cursor:
        def fetchall(self):
            return [self.fetchone()]

        def fetchone(self):
            return {'id': 9, 'order_id': None, 'shop_id': None, 'shop_name': None,
                    'category': 'delivery', 'summary': '已登记', 'status': 'open',
                    'created_at': 'before', 'updated_at': 'now',
                    'detail': '详情', 'contact_phone': '13800000000'}

    class Connection:
        def execute(self, sql, params):
            observed.append(('execute', threading.get_ident(), sql, params))
            return Cursor()

    @contextmanager
    def transaction():
        observed.append(('enter', threading.get_ident(), None, None))
        entered.set()
        assert release.wait(2), 'database gate was not opened by the event loop'
        try:
            yield Connection()
        finally:
            observed.append(('exit', threading.get_ident(), None, None))

    monkeypatch.setattr(merchant_tickets, 'transaction', transaction)

    async def scenario():
        task = asyncio.create_task(merchant_tickets.get_my_ticket(9, user_id='owner') if detail
                                   else merchant_tickets.list_my_tickets(user_id='owner'))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 0.5)
        finally:
            release.set()
        return await asyncio.wait_for(task, 2)

    response = asyncio.run(scenario())
    assert response['data']['detail' if detail else 'items']
    assert [entry[0] for entry in observed] == ['enter', 'execute', 'exit']
    assert all(entry[1] != loop_thread and entry[1] == observed[0][1] for entry in observed)
    assert 'user_id = ?' in observed[1][2]
    assert observed[1][3][0 if not detail else 1] == 'owner'


def test_missing_or_other_owner_ticket_is_indistinguishable(monkeypatch):
    monkeypatch.setattr(merchant_tickets, '_get_ticket_row', lambda ticket_id, uid: None)
    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await merchant_tickets.get_my_ticket(7, user_id='owner')
        assert exc.value.status_code == 404
    asyncio.run(scenario())
