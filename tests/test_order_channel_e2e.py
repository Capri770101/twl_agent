"""下单写通道离线 e2e：mock 平台下单 API 驱动完整 run()，验证交易闭环代码侧。

与 test_customer_e2e_offline 同款哲学：LLM 用轮次脚本、存储用内存替身，但
**execute_tool 与 create_order 工具层不打桩**——真实经过 toolkit 门禁 →
skill_order payload 组装 → httpx 提交（trust_env=False）→ 响应解析 →
order_card/pay_jump UI 推导 → trade 护栏凭真实工具证据放行/拦截。

覆盖：
1. happy path：DIY 方案下单 → pay_jump UI + 真实 order_id + payload 契约字段；
2. 平台 500：模型仍谎称「下单成功」→ 护栏额度用尽 → 发布前替换为如实说明；
3. 幂等：同 request_id 重复提交返回同一订单；
4. 鉴权：require Bearer 时缺 Key 被拒、有 Key 通过；
5. 响应缺 order_id / 业务失败：错误如实透传，不产出订单卡。

⚠️ 离线契约验证 ≠ 真实平台联调：字段名/金额单位/支付跳转参数须等平台 API
文档到位后对齐（DELIVERY §5），mock 契约以 skill_order.py 文档字符串为准。
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

import agent.agent as A
import agent.skills.skill_order as SO
import agent.spine as SPINE
import agent.tools as TOOLS
from agent.engine.understand import Understanding
from backend.config import settings
from scripts.mock_platform_order import Handler
from tests.test_run_e2e import FakeMemory, _chunk, _tc

# ── 会话内已确认的 DIY 方案（create_order plan_id='latest' 经 selected_plan 解析）──
SEEDED_DIY_PLAN = {
    'plan_id': 'DIY_seed', 'name': '暖阳如初', 'diy': True, 'price': 199,
    'desc': '香槟玫瑰9支配满天星，适合送母亲',
    'design': {
        'main_flowers': [{'name': '香槟玫瑰', 'qty': 9}],
        'fillers': [{'name': '满天星', 'qty': 3}],
        'foliage': [{'name': '尤加利叶', 'qty': 2}],
    },
    'requirement': '送妈妈，预算200',
}


class SeededMemory(FakeMemory):
    """FakeMemory + 按 key 返回预置会话 JSON（selected_plan / latest_diy_plan）。"""

    def __init__(self, session_json: dict | None = None) -> None:
        super().__init__()
        self._session_json = session_json or {}

    async def get_session_json(self, uid, sid, key):
        return self._session_json.get(key, {})


@pytest.fixture
def order_backend(monkeypatch):
    """起 mock 平台下单 API + 接通下单写通道（开关 + URL 双条件）。"""
    Handler.reset()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_ENABLED', True)
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_API_URL', f'http://127.0.0.1:{port}/v1/orders')
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_API_KEY', '')
    yield port
    server.shutdown()
    t.join(timeout=5)


def _install_full_stack(monkeypatch, turns, port):
    """内存存储 + 理解层(buying) + 轮次脚本 LLM；工具层保持真实。"""
    mem = SeededMemory({'selected_plan': SEEDED_DIY_PLAN, 'latest_diy_plan': SEEDED_DIY_PLAN})
    monkeypatch.setattr(A, 'mem_store', mem, raising=False)
    # _resolve_session_plan 走 agent.tools 的 memory 绑定，须同步替换
    monkeypatch.setattr(TOOLS, 'memory', mem, raising=True)
    monkeypatch.setattr(A, '_find_pending_image_task', lambda *a, **k: _none(), raising=False)
    monkeypatch.setattr(A, '_platform_source_ids', lambda: [], raising=False)
    monkeypatch.setattr(SPINE, 'understand',
                        lambda *a, **k: Understanding(intent='buying'), raising=True)
    # DIY 落库依赖真实 DB：桩掉（skill_order 内本就是 best-effort，失败不影响提交平台）
    import backend.storage.diy as DIY

    async def _fake_save(plan, user_id):
        return {'saved': True, 'duplicate': False, 'plan_id': plan.get('plan_id') or 'DIY_seed'}

    monkeypatch.setattr(DIY, 'save_diy_plan', _fake_save, raising=True)
    monkeypatch.setattr(DIY, 'mark_diy_plan_ordered', lambda *a, **k: None, raising=False)
    monkeypatch.setattr(DIY, 'record_plan_confirmed', lambda *a, **k: None, raising=False)

    calls = {'n': 0}

    def fake_stream(*a, **k):
        i = calls['n']
        calls['n'] += 1
        return iter(turns[min(i, len(turns) - 1)])

    monkeypatch.setattr(A, 'call_llm_stream', fake_stream, raising=False)
    return mem


async def _none():
    return None


def _run(message: str):
    agent = A.ReActAgent()
    events: list[dict] = []

    async def _go():
        return await asyncio.wait_for(
            agent.run('u_buy', message, 'sid_buy', None, on_event=events.append),
            timeout=30)
    return asyncio.run(_go()), events


# ── 1. happy path：完整交易闭环 ──

def test_diy_order_full_loop_produces_pay_jump(order_backend, monkeypatch):
    """模型调 create_order（真工具→mock 平台）→ 声称已下单 → 护栏凭证据放行 → pay_jump。"""
    turns = [
        [_chunk(tool_calls=[_tc(0, 'c1', 'create_order',
                                '{"shop_id":"s007","plan_id":"latest","plan_type":"diy"}')])],
        [_chunk(tool_calls=[_tc(0, 'c2', 'respond_to_user',
                                '{"reply":"已经为你下单成功啦，点下方按钮完成支付就好～",'
                                '"ui":"text","data":{},"stage":"done","intent":"buying"}')])],
    ]
    _install_full_stack(monkeypatch, turns, order_backend)
    resp, events = _run('帮我下单，就刚才挑的那个')

    # 真实工具证据 → trade 护栏不得替换「下单成功」声称
    assert '下单成功' in (resp.reply or ''), f'有真实订单证据时应放行声称：{resp.reply!r}'
    # UI：create_order 结果带 pay_jump → PAY_JUMP（_post_process 分支）
    assert getattr(resp.ui, 'value', resp.ui) == 'pay_jump'
    assert str(resp.data.get('order_id', '')).startswith('PMOCK')
    assert resp.data['pay_jump']['page_path'] == '/pages/order/confirm'
    # 工具记录与 SSE card 事件
    order_calls = [tc for tc in resp.tool_calls if tc.name == 'create_order']
    assert order_calls and order_calls[0].status == 'ok'
    assert any(e.get('event') == 'card' and e.get('ui') == 'pay_jump' for e in events)

    # payload 契约（skill_order._build_payload 的部署约定）
    assert len(Handler.received) == 1
    payload = Handler.received[0]
    assert payload['request_id'].startswith('AG')
    assert payload['channel'] == 'flora_agent'
    assert payload['external_user_id'] == 'u_buy'
    assert payload['shop_id'] == 's007'
    assert payload['plan']['type'] == 'diy'
    assert payload['estimated_total'] == 199.0
    flowers = {(i['name'], i['qty']) for i in payload['items'] if i.get('kind') == 'flower'}
    assert flowers == {('香槟玫瑰', 9), ('满天星', 3), ('尤加利叶', 2)}


# ── 2. 平台故障：不得谎称成功 ──

def test_platform_http_failure_yields_honest_reply(order_backend, monkeypatch):
    """平台 500 → 工具 error → 模型仍谎称已下单 → 额度用尽后发布前替换为如实说明。"""
    Handler.mode = 'http500'
    claim = '{"reply":"已经为你下单成功，订单已生成。","ui":"text","data":{},"stage":"done"}'
    turns = [
        [_chunk(tool_calls=[_tc(0, 'c1', 'create_order',
                                '{"shop_id":"s007","plan_id":"latest","plan_type":"diy"}')])],
        [_chunk(tool_calls=[_tc(0, 'c2', 'respond_to_user', claim)])],
    ]
    _install_full_stack(monkeypatch, turns, order_backend)
    resp, events = _run('帮我下单，就刚才挑的那个')

    assert '下单成功' not in (resp.reply or ''), '平台拒绝时不得发布成功声称'
    assert '说法不准确' in (resp.reply or ''), f'应替换为如实说明：{resp.reply!r}'
    assert getattr(resp.ui, 'value', resp.ui) == 'text'
    order_calls = [tc for tc in resp.tool_calls if tc.name == 'create_order']
    assert order_calls and order_calls[0].status == 'error'
    assert '500' in str(order_calls[0].result)


# ── 3-6. 契约单测（直接打 mock / 直接调工具函数）──

def test_idempotent_request_id_returns_same_order(order_backend):
    payload = {'request_id': 'AG_TEST_IDEM_1', 'channel': 'flora_agent',
               'external_user_id': 'u1', 'agent_session_id': 's1', 'shop_id': 's007',
               'plan': {'plan_id': 'P1', 'name': 'x', 'type': 'existing', 'price': 99.0},
               'items': [{'kind': 'plan', 'plan_id': 'P1', 'name': 'x', 'qty': 1, 'price': 99.0}],
               'estimated_total': 99.0, 'remark': ''}
    url = f'http://127.0.0.1:{order_backend}/v1/orders'
    with httpx.Client(trust_env=False, timeout=5) as c:
        r1 = c.post(url, json=payload).json()
        r2 = c.post(url, json=payload).json()
    assert r1['order_id'] == r2['order_id'], '同 request_id 必须幂等返回同一订单'
    assert r1['order_id'].startswith('PMOCK')


def test_auth_header_enforced_when_key_configured(order_backend, monkeypatch):
    """mock 要求 Bearer 头：未配 Key → 401 可读错误；配了 Key → 通过。"""
    Handler.require_auth = True
    payload = {'request_id': 'AG_TEST_AUTH_1', 'shop_id': 's007',
               'plan': {'plan_id': 'P1', 'type': 'existing', 'price': 99.0},
               'items': [], 'estimated_total': 99.0}
    # 未配 Key → 401 → RuntimeError 含状态码（skill_order 转为 error JSON 返回模型）
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_API_KEY', '')
    with pytest.raises(RuntimeError, match='401'):
        asyncio.run(SO._submit_platform_order(payload))
    # 配了 Key → Bearer 头随请求发出 → 成功
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_API_KEY', 'dev_order_key')
    result = asyncio.run(SO._submit_platform_order(payload))
    assert result['order_id'].startswith('PMOCK')


def test_missing_order_id_reported_honestly(order_backend, monkeypatch):
    Handler.mode = 'no_order_id'
    mem = SeededMemory({'selected_plan': SEEDED_DIY_PLAN, 'latest_diy_plan': SEEDED_DIY_PLAN})
    monkeypatch.setattr(TOOLS, 'memory', mem, raising=True)
    import backend.storage.diy as DIY

    async def _fake_save(plan, user_id):
        return {'saved': True, 'duplicate': False, 'plan_id': 'DIY_seed'}
    monkeypatch.setattr(DIY, 'save_diy_plan', _fake_save, raising=True)
    out = json.loads(asyncio.run(SO.create_order(
        's007', 'latest', 'diy', _context={'user_id': 'u1', 'session_id': 'sid1'})))
    assert 'error' in out and '缺少订单号' in out['error']


def test_business_error_surfaced(order_backend, monkeypatch):
    Handler.mode = 'business_error'
    mem = SeededMemory({'selected_plan': SEEDED_DIY_PLAN, 'latest_diy_plan': SEEDED_DIY_PLAN})
    monkeypatch.setattr(TOOLS, 'memory', mem, raising=True)
    import backend.storage.diy as DIY

    async def _fake_save(plan, user_id):
        return {'saved': True, 'duplicate': False, 'plan_id': 'DIY_seed'}
    monkeypatch.setattr(DIY, 'save_diy_plan', _fake_save, raising=True)
    out = json.loads(asyncio.run(SO.create_order(
        's007', 'latest', 'diy', _context={'user_id': 'u1', 'session_id': 'sid1'})))
    assert 'error' in out and '库存不足' in out['error']
