import asyncio
import json
from unittest.mock import AsyncMock
from agent import customer_tools
from agent.toolkit import TOOL_REGISTRY, visible_tool_specs
from backend.config import settings


def test_credential_only_from_context(monkeypatch):
    call = AsyncMock(return_value={'ok': False, 'code': 'AUTH_REQUIRED'})
    monkeypatch.setattr(customer_tools, 'query_orders', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    result = asyncio.run(customer_tools.query_my_orders('order-1', _context={'customer_credential': 'cs1.private'}))
    call.assert_awaited_once_with('cs1.private', order_id='order-1', page=1)
    assert 'private' not in result
    assert json.loads(result)['code'] == 'AUTH_REQUIRED'
    props = TOOL_REGISTRY['query_my_orders'].parameters['properties']
    assert not {'user_id', 'credential', 'token'} & props.keys()


def test_feature_off_hides_private_tool(monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', False)
    assert 'query_my_orders' not in {s.name for s in visible_tool_specs()}
