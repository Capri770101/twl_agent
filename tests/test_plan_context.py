"""方案上下文注入回归门：历史方案卡 → system prompt 中的「权威方案」摘要。

背景：历史消息的 ``data``（方案结构）不会作为可读内容发给模型，模型只能靠上一条回复的
文字「回忆」方案——线上实测出现过把「粉康乃馨×6 + 粉洋桔梗×5」说成「粉佳人玫瑰配粉绣球」
的失真。本文件锁住「方案要点必须被显式注入」这一行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.agent import ReActAgent, _latest_plan_summary


def _plan_msg(name: str, price: float, flowers: list[dict], colors: str) -> dict:
    return {
        'role': 'assistant',
        'content': '方案已生成',
        'ui': 'plan_card',
        'data': {'plans': [{
            'name': name,
            'budget_num': price,
            'design': {'main_flowers': flowers, 'color_scheme': colors},
        }]},
    }


# ── _latest_plan_summary ──

def test_no_history_returns_empty():
    assert _latest_plan_summary([]) == ''
    assert _latest_plan_summary(None) == ''  # type: ignore[arg-type]


def test_text_message_returns_empty():
    hist = [{'role': 'assistant', 'content': '你好', 'ui': 'text', 'data': {}}]
    assert _latest_plan_summary(hist) == ''


def test_summary_contains_key_facts():
    s = _latest_plan_summary([_plan_msg(
        '粉韵温情', 200,
        [{'name': '康乃馨', 'qty': 6}, {'name': '洋桔梗', 'qty': 5}],
        '粉色系',
    )])
    assert '粉韵温情' in s
    assert '200' in s
    assert '康乃馨×6' in s and '洋桔梗×5' in s
    assert '粉色系' in s


def test_latest_plan_wins():
    """多轮改方案后，注入的必须是**最新**那版，否则模型会拿着旧方案回答。"""
    older = _plan_msg('旧方案', 100, [{'name': '玫瑰', 'qty': 11}], '红色系')
    newer = _plan_msg('新方案', 300, [{'name': '绣球', 'qty': 3}], '白色系')
    s = _latest_plan_summary([older, {'role': 'user', 'content': '换一个'}, newer])
    assert '新方案' in s and '旧方案' not in s


def test_malformed_plans_are_safe():
    hist = [{'role': 'assistant', 'content': '', 'data': {'plans': [None, {}, {'name': ''}, 'x']}}]
    assert _latest_plan_summary(hist) == ''


def test_multi_plan_card_summarized():
    hist = [{
        'role': 'assistant', 'content': '', 'ui': 'plan_card',
        'data': {'plans': [
            {'name': 'A', 'budget_num': 100, 'design': {}},
            {'name': 'B', 'budget_num': 200, 'design': {}},
        ]},
    }]
    s = _latest_plan_summary(hist)
    assert 'A' in s and 'B' in s


# ── _build_system 注入 ──

def test_build_system_injects_plan():
    out = ReActAgent._build_system(  # type: ignore[arg-type]
        None, None, {}, current_plan='「粉韵温情」（参考价 200 元；主花：康乃馨×6）',
    )
    assert '当前方案' in out
    assert '康乃馨×6' in out
    assert 'revise_diy_plan' in out  # 改方案必须调工具，而不是只写文字


def test_build_system_omits_plan_when_empty():
    out = ReActAgent._build_system(None, None, {}, current_plan='')  # type: ignore[arg-type]
    assert '会话中的当前方案' not in out
