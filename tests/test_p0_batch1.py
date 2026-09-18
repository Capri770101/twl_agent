"""第一批 P0 修复的回归测试（2026-09-18 外部安全审计）。

覆盖三项：
1. `reply` 危险 HTML 标签中和（XSS 兜底）—— 且**不能误伤正常文本**；
2. AIGC 内容标识（GB 45438-2025）—— 非流式响应必须带标识字段，文案单一来源；
3. 公网接口文档开关 —— 默认关闭（/docs、/redoc、/openapi.json 不暴露）。
"""

from __future__ import annotations

import re

import pytest

from agent.agent import _neutralize_dangerous_tags
from agent.engine.ui_protocol import AI_CONTENT_DISCLOSURE, ChatResponse

# ── 1. XSS 危险标签中和 ────────────────────────────────────────────────────

XSS_PAYLOADS = [
    '<script>alert(1)</script>',
    '<SCRIPT>alert(1)</SCRIPT>',
    '<img src=x onerror=alert(1)>',
    '<iframe src="javascript:alert(1)"></iframe>',
    '<svg onload=alert(1)>',
    '<body onload=alert(1)>',
    '<a href="javascript:alert(1)">点我</a>',
    '<style>body{display:none}</style>',
    '<form action=x><input name=a></form>',
    '<object data=x></object>',
    '<embed src=x>',
    '<link rel=stylesheet href=x>',
    '<meta http-equiv=refresh content=0>',
    '<details open ontoggle=alert(1)>',
    '<video><source onerror=alert(1)></video>',
    '<  script >alert(1)</ script >',   # 空格绕过
]

_TAG_START = re.compile(r'<\s*/?\s*[a-zA-Z]')


@pytest.mark.parametrize('payload', XSS_PAYLOADS)
def test_xss_payload_neutralized(payload: str) -> None:
    """中和后不能再残留任何可执行标签。"""
    out = _neutralize_dangerous_tags(payload)
    assert not _TAG_START.search(out), f'仍残留标签：{out!r}'


def test_original_incident_reproduced_then_fixed() -> None:
    """审计场景：用户发 `<script>`，模型若复述原文，标签就会进 reply。"""
    reply = '你发的这段 <script>alert(1)</script> 是网页代码，不是花的照片哦。'
    out = _neutralize_dangerous_tags(reply)
    assert '<script>' not in out
    assert '&lt;script&gt;' in out
    # 正文必须保留（不能因为中和把内容删掉）
    assert '是网页代码，不是花的照片哦' in out


NORMAL_TEXTS = [
    '这束花主花是粉洋桔梗 6 支，配满天星点缀，预算约 290 元。',
    '我喜欢 <3 这种感觉的表达方式',                     # 非标签的尖括号：不该被全量转义
    '1 + 1 < 3 这个数学表达式',
    '价格是 100 > 80，所以选前者更划算',
    '收到后斜剪根部 45°，深水醒花 2-4 小时。',
    '',
]


@pytest.mark.parametrize('text', NORMAL_TEXTS)
def test_normal_text_untouched(text: str) -> None:
    """关键取舍：不做全量转义 —— 正常文本里的 < > 必须原样保留。"""
    assert _neutralize_dangerous_tags(text) == text


def test_non_string_safe() -> None:
    assert _neutralize_dangerous_tags(None) is None   # type: ignore[arg-type]


# ── 2. AIGC 内容标识（GB 45438-2025） ─────────────────────────────────────

def test_chat_response_carries_aigc_label() -> None:
    """接入方要能直接从响应里拿到标识，而不是自己猜。"""
    d = ChatResponse(user_id='u1', reply='你好').model_dump()
    assert d['ai_generated'] is True
    assert d['content_disclosure'] == AI_CONTENT_DISCLOSURE
    assert 'AI' in d['content_disclosure']


def test_disclosure_text_is_single_source() -> None:
    """文案必须单一来源，避免 /chat 与 /chat/stream 口径漂移。"""
    assert ChatResponse(user_id='u1').content_disclosure == AI_CONTENT_DISCLOSURE


def test_label_present_even_for_empty_reply() -> None:
    """即使回复为空（异常兜底场景），标识也必须带上。"""
    d = ChatResponse(user_id='u1').model_dump()
    assert d['ai_generated'] is True


# ── 3. 公网接口文档开关 ───────────────────────────────────────────────────

def test_api_docs_disabled_by_default() -> None:
    """匿名可取 /docs 会暴露全部端点 schema（审计 P0），默认必须关闭。"""
    import main

    assert main.app.docs_url is None, '/docs 仍暴露'
    assert main.app.redoc_url is None, '/redoc 仍暴露'
    assert main.app.openapi_url is None, '/openapi.json 仍暴露'


def test_health_returns_minimal_info() -> None:
    """/health 不得泄露 env / version（便于按版本找已知漏洞）。"""
    import asyncio

    import main

    # 用**独立**事件循环：全量跑时可能已有 loop 在运行，get_event_loop() 会 RuntimeError。
    loop = asyncio.new_event_loop()
    try:
        payload = loop.run_until_complete(main.health())
    finally:
        loop.close()
    assert payload == {'status': 'ok'}
    for leaked in ('env', 'version', 'service'):
        assert leaked not in payload, f'/health 仍泄露 {leaked}'
