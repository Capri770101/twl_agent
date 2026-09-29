import asyncio
import json
from unittest.mock import AsyncMock
import pytest
import agent.agent as module
from agent.agent import ReActAgent, SessionStage
from agent.engine.ui_protocol import ToolCallRecord


@pytest.mark.parametrize('name,result,expected', [
    ('query_shop_service', {'ok': True, 'data': {'shops': [{'id':'s1'}]}}, 'customer_shops'),
    ('query_my_orders', {'ok': False, 'code':'AUTH_REQUIRED'}, 'customer_login'),
    ('query_my_orders', {'ok': True, 'data': {'items':[{'id':'o1'}]}}, 'customer_orders'),
    ('query_my_orders', {'ok': False, 'code':'UPSTREAM_TIMEOUT'}, 'text'),
])
def test_only_real_results_create_business_cards(monkeypatch, name, result, expected):
    for method in ('get_session_flag','set_session_flag','clear_session_flags'):
        monkeypatch.setattr(module.mem_store, method, AsyncMock(return_value=None))
    monkeypatch.setattr(module, '_clarify_slots', lambda *a, **kw: [])
    record = ToolCallRecord(name=name, arguments={}, result=json.dumps(result), status='ok')
    response = {'ui':'customer_orders', 'data':{'items':[{'id':'fabricated'}]}, 'reply':'查询结果'}
    _, ui, data, _, _ = asyncio.run(ReActAgent()._post_process(response, [record], SessionStage.ANALYZE, '查信息', '', 'u','s',None,[]))
    assert str(ui) == expected
    assert 'fabricated' not in json.dumps(data)
