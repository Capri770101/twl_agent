"""客服场景离线 e2e：mock-LLM 驱动完整 run()，工具层打到本地 mock H5 后端。

为什么重要
----------
真实 H5 服务器已封禁（2026-10-08），客户服务链路的端到端回归只能离线做：
LLM 用轮次脚本（同 test_run_e2e 的 `_install` 模式），但 **execute_tool 不打桩** ——
真工具经 `CUSTOMER_SERVICE_ALLOW_INSECURE`（dev）打到本测试内嵌的 mock 后端。

这样一次覆盖：意图路由 scope → 工具注册可见性 → dev HTTP 放行 → 契约互操作
（cs1 票据）→ 卡片要点兜底（2026-10-08 线上空壳 bug 的回归）。
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from http.server import ThreadingHTTPServer

import pytest

import agent.agent as A
from scripts.mock_customer_service import Handler as MockHandler


@pytest.fixture
def mock_backend(monkeypatch, tmp_path):
    """起内嵌 mock H5 后端 + 把 settings 指过去（dev + HTTP 放行）。"""
    MockHandler.secret = b'dev_secret_at_least_32_bytes_padding!!'
    server = ThreadingHTTPServer(('127.0.0.1', 0), MockHandler)  # 随机端口
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    from backend.config import settings
    monkeypatch.setattr(settings, 'APP_ENV', 'dev')
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_BASE_URL', f'http://127.0.0.1:{port}/api/customer-service')
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_SECRET', 'dev_secret_at_least_32_bytes_padding!!')
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ALLOW_INSECURE', True)
    yield port
    server.shutdown()
    t.join(timeout=5)


def _chunk(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, tool_calls=tool_calls))])


def _tc(index=0, id=None, name=None, args=None):
    return SimpleNamespace(index=index, id=id,
                           function=SimpleNamespace(name=name, arguments=args))


class _Mem:
    def __getattr__(self, item):
        async def _noop(*a, **k):
            if item in ('get_or_create_session', 'create_conversation'):
                return 'sid_cs'
            if item == 'get_session_context':
                return {}
            if item == 'get_stage':
                return 'analyze'
            if item == 'get_long_term':
                return {}
            if item == 'get_session_json':
                return {}
            if item == 'load_history':
                return []
            if item == 'load_display_messages':
                return []
            if item == 'get_requirement':
                return None
            if item == 'get_session_flag':
                return None
            return None
        return _noop


def _install_llm(monkeypatch, turns):
    """轮次脚本驱动 LLM；工具与内存不打桩（工具层走真实实现）。

    v2 靶点（2026-10-14 v1 退役）：理解层替身固定返回 None（降级 = 全量工具，
    与生产理解失败语义一致），保证离线测试不依赖理解层 LLM 也不受环境 Key 影响。
    """
    monkeypatch.setattr(A, 'mem_store', _Mem(), raising=False)
    monkeypatch.setattr(A, '_find_pending_image_task', lambda *a, **k: _none(), raising=False)
    monkeypatch.setattr(A, '_platform_source_ids', lambda: [], raising=False)
    import agent.spine as SPINE
    monkeypatch.setattr(SPINE, 'understand', lambda *a, **k: None, raising=True)
    calls = {'n': 0}

    def fake_stream(*a, **k):
        i = calls['n']
        calls['n'] += 1
        return iter(turns[min(i, len(turns) - 1)])
    monkeypatch.setattr(A, 'call_llm_stream', fake_stream, raising=False)


async def _none():
    return None


def _run(message: str, credential: str = ''):
    agent = A.ReActAgent()

    async def _go():
        return await asyncio.wait_for(
            agent.run('u_cs', message, 'sid_cs', None, on_event=lambda e: None,
                      customer_credential=credential),
            timeout=30)
    return asyncio.run(_go())


def test_shop_consultation_real_tool(mock_backend, monkeypatch):
    """店铺咨询：模型调 query_shop_service（真工具打到 mock）→ 卡片 + 要点非空壳。"""
    turns = [
        [_chunk(tool_calls=[_tc(0, 'c1', 'query_shop_service', '{"shop_id": "s007"}')])],
        [_chunk(tool_calls=[_tc(0, 'c2', 'respond_to_user',
                                '{"reply": "", "ui": "customer_shops", "data": {}, "stage": "analyze"}')])],
    ]
    _install_llm(monkeypatch, turns)
    resp = _run('花诺花艺几点营业？地址在哪？')
    ui = getattr(resp.ui, 'value', resp.ui)
    assert ui == 'customer_shops', f'ui={ui}'
    # 回合里模型给空 reply → 要点兜底必须基于真实工具数据生成，不能是空壳
    assert '花诺花艺' in (resp.reply or ''), f'reply={resp.reply!r}'
    assert '08:00-22:00' in (resp.reply or ''), '卡片要点应含营业时间'
    shops = (resp.data or {}).get('shops') or []
    assert any(s.get('id') == 's007' for s in shops), '卡片 data 应含真实门店'


def test_my_orders_real_tool(mock_backend, monkeypatch):
    """我的订单：cs1 票据（mock 签发）→ 真工具查询 → 订单要点。"""
    # 从 mock 换票（trust_env=False：开发机系统代理会劫持 127.0.0.1 请求成 502 空 body）
    import httpx
    with httpx.Client(timeout=5, trust_env=False) as c:
        r = c.post(f'http://127.0.0.1:{mock_backend}/api/customer-service/credential')
        token = r.json()['data']['access_token']
    turns = [
        [_chunk(tool_calls=[_tc(0, 'c1', 'query_my_orders', '{}')])],
        [_chunk(tool_calls=[_tc(0, 'c2', 'respond_to_user',
                                '{"reply": "", "ui": "customer_orders", "data": {}, "stage": "analyze"}')])],
    ]
    _install_llm(monkeypatch, turns)
    resp = _run('我的订单到哪了？', credential=token)
    ui = getattr(resp.ui, 'value', resp.ui)
    assert ui == 'customer_orders', f'ui={ui}'
    items = (resp.data or {}).get('items') or []
    assert items, '订单卡 data 必须有真实订单'
    assert any('订单' in (resp.reply or '') or 'MOCK' in (resp.reply or '') for _ in [0]), f'reply={resp.reply!r}'
    assert (resp.reply or '').strip(), '要点兜底后 reply 不得为空'


def test_my_orders_no_credential(mock_backend, monkeypatch):
    """未登录：真工具返回 AUTH_REQUIRED → 模型引导登录，不得说没有订单。"""
    turns = [
        [_chunk(tool_calls=[_tc(0, 'c1', 'query_my_orders', '{}')])],
        [_chunk(tool_calls=[_tc(0, 'c2', 'respond_to_user',
                                '{"reply": "查询订单需要先登录哦，点下方登录后回来继续问我就行～", "ui": "text", "data": {}}')])],
    ]
    _install_llm(monkeypatch, turns)
    resp = _run('查一下我的订单。', credential='')
    reply = resp.reply or ''
    assert '登录' in reply, f'应引导登录: {reply!r}'
    assert '没有订单' not in reply, '未登录不得说成没有订单'


