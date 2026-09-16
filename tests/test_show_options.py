"""show_options 工具 + 追问「模型主导」的回归测试。

背景（2026-09-16，Capri 要求「文字之外的功能都要是工具调用形式」）：
- 「给用户出选项」原先只能靠 respond_to_user(ui="dialog_options", data={...}) 手拼，
  模型既容易拼错、也想不到有这个能力 → 独立成 show_options 工具；
- L3 澄清追问原先一律追加固定模板句，是明显的机械感来源 → 改为模型自己问了就不追加。
"""

from __future__ import annotations

import pytest

from agent.agent import _TERMINAL_TOOLS, _append_clarify
from agent.tools import show_options
from agent.toolkit import execute_tool, visible_tool_specs


def test_show_options_registered_and_visible() -> None:
    """新工具必须对 C 端可见（否则模型看不到这个能力）。"""
    names = {s.name for s in visible_tool_specs()}
    assert 'show_options' in names


def test_show_options_is_terminal_tool() -> None:
    """它必须被登记为终结工具，否则 respond_args 提取不到、UI 不会生效。"""
    assert 'show_options' in _TERMINAL_TOOLS


def test_show_options_normalizes_string_labels() -> None:
    out = show_options(['温柔韩式', '清新自然'])
    assert out['ui'] == 'dialog_options'
    assert out['data']['options'] == [
        {'label': '温柔韩式', 'value': '温柔韩式'},
        {'label': '清新自然', 'value': '清新自然'},
    ]


def test_show_options_accepts_dict_items_with_hint() -> None:
    out = show_options([{'label': '韩式', 'value': 'korean', 'hint': '偏高级感'}])
    assert out['data']['options'][0] == {'label': '韩式', 'value': 'korean', 'hint': '偏高级感'}


def test_show_options_drops_blank_items() -> None:
    out = show_options(['  ', '', None, '有效项'])
    assert len(out['data']['options']) == 1
    assert out['data']['options'][0]['label'] == '有效项'


def test_show_options_illegal_enums_downgrade() -> None:
    """非法枚举必须降级，不能让脏值流到下游。"""
    out = show_options(['A'], intent='bogus', confirmation='bogus', image='bogus')
    assert out['intent'] == 'other'
    assert out['confirmation'] == 'none'
    assert out['image'] == 'none'


def test_show_options_reply_passthrough() -> None:
    out = show_options(['A'], reply='想往哪个方向调？')
    assert out['reply'] == '想往哪个方向调？'


def test_show_options_via_execute_tool() -> None:
    """走工具执行层也要能正常返回（模型实际调用的路径）。"""
    import asyncio
    import json

    raw, status = asyncio.run(execute_tool('show_options', {'options': ['A', 'B']}, {}))
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert status == 'ok'
    assert payload['ui'] == 'dialog_options'
    assert len(payload['data']['options']) == 2


# ── L3 追问：模型主导，模板只兜底 ──────────────────────────────────────────

def test_clarify_not_appended_when_model_already_asked() -> None:
    """模型自己写了问句 → 一个字都不追加（机械感的根源就在这里）。"""
    reply = '想送你妈妈的话，方便说下预算大概多少吗？'
    assert _append_clarify(reply, ['recipient', 'budget']) == reply


def test_clarify_appended_when_model_forgot_to_ask() -> None:
    """模型只陈述、没提问 → 系统兜底补一句（保证信息确实被问出来）。"""
    out = _append_clarify('这个方向我可以做。', ['budget'])
    assert out.startswith('这个方向我可以做。')
    assert '预算' in out


def test_clarify_empty_reply_still_asks() -> None:
    out = _append_clarify('', ['recipient'])
    assert '送给谁' in out
