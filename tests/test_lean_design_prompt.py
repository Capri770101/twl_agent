"""延迟护栏：设计类 LLM 调用只能要**规则引擎做不到的语义字段**。

背景（2026-09-20 实测，服务器容器内）：
- LLM 耗时 ≈ 输出 token × 17~19ms，**上下文长度几乎无影响**
  （塞到 2 万字上下文首字仍只要 1.76s）；
- 原 schema 要求模型输出 793 token（含 effect_prompt / diy_steps / care_tips /
  shelf_life / difficulty …）→ 15.0s；
- 精简到 424 token → 7.2s（省 52%）。

这些被删掉的字段**baseline 全都算好了**，_merge_plan 是「LLM 缺则回落 baseline」，
所以不写反而更快、质量不降。尤其 `effect_prompt`：它在 _merge_plan 末尾会被
`_effect_prompt_from_design` 按**校正后的支数**重建，模型写了纯属白烧 token。

⚠️ 本测试是**性能护栏**：如果将来有人「好心」把字段加回 prompt，
这里会失败，提醒他先看完上面的实测数据。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent.tools as T  # noqa: E402

# 规则引擎已算好、模型不该再写的字段（写在 prompt 里 = 白烧 token）
REDUNDANT_IN_PROMPT = [
    'effect_prompt', 'diy_steps', 'care_tips', 'card_message',
    'difficulty', 'est_time', 'shelf_life', 'suitable_for', 'caution', 'mood_tags',
]
# 模型真正独有、必须保留的语义字段
KEEP_IN_PROMPT = ['"name"', '"style"', '"desc"', '"color_scheme"', '"packaging"', '"meaning"']


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]


def _capture_system(monkeypatch: pytest.MonkeyPatch, fn_name: str, payload: str) -> str:
    """拦截 call_llm，返回它收到的 system prompt。"""
    captured: dict[str, str] = {}

    def _fake(messages, **kwargs):  # noqa: ANN001
        captured['system'] = messages[0]['content']
        return _FakeResp(payload)

    monkeypatch.setattr(T, 'call_llm', _fake, raising=False)
    # RAG 检索绕开（与本测试无关，且会读知识库拖慢）
    monkeypatch.setattr(T, '_retrieve_for_design', lambda *a, **k: '【候选花材】玫瑰、康乃馨', raising=False)
    getattr(T, fn_name)('送妈妈一束花，预算300')
    return captured.get('system', '')


def test_design_prompt_omits_redundant_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """设计 prompt 不该要求模型输出 baseline 已算好的字段。"""
    system = _capture_system(monkeypatch, 'design_with_llm', '{"name":"测试"}')
    leaked = [f for f in REDUNDANT_IN_PROMPT if f in system]
    assert not leaked, (
        f'这些字段 baseline 已经算好，写进 prompt 只会让模型多写 token、拖慢响应：{leaked}。'
        '如果确实要恢复，请先看 tests/test_lean_design_prompt.py 顶部的实测数据。'
    )


def test_design_prompt_keeps_semantic_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """精简不能精简到「模型独有的语义字段」——那才是调 LLM 的意义。"""
    system = _capture_system(monkeypatch, 'design_with_llm', '{"name":"测试"}')
    missing = [f for f in KEEP_IN_PROMPT if f not in system]
    assert not missing, f'这些是模型独有的语义字段，不能从 prompt 里删掉：{missing}'


# ── 精简输出后，_merge_plan 必须把卡片字段补齐（不能省到用户看得见的东西）──

LEAN_LLM_PLAN = {
    'name': '暖阳絮语',
    'style': '韩式',
    'desc': '玫瑰×11 配满天星×3',
    'design': {
        'main_flowers': [{'name': '玫瑰', 'qty': 11}],
        'fillers': [{'name': '满天星', 'qty': 3}],
        'foliage': [{'name': '尤加利', 'qty': 2}],
        'color_scheme': ['香槟', '粉'],
        'packaging': '雾面牛皮纸',
        'meaning': '细水长流的陪伴',
    },
}
DIMS = {'recipient': '妈妈', 'occasion': '生日', 'budget': '300', 'scene': '生日'}


@pytest.fixture
def merged() -> dict:
    return T._merge_plan(T._build_plan(dict(DIMS)), LEAN_LLM_PLAN)


@pytest.mark.parametrize('key', [
    'plan_id', 'name', 'style', 'recipient', 'occasion', 'desc', 'effect_prompt',
    'price', 'price_text', 'estimated_price', 'budget_tier',
    'diy_steps', 'care_tips', 'card_message', 'budget_breakdown',
])
def test_top_level_fields_survive_lean_output(merged: dict, key: str) -> None:
    """顶层字段必须全部有值（模型不给 → 回落 baseline）。"""
    assert merged.get(key) not in (None, '', [], {}), f'{key} 在精简输出后丢了'


@pytest.mark.parametrize('key', [
    'main_flowers', 'fillers', 'foliage', 'color_scheme', 'packaging', 'meaning',
    'diy_steps', 'care_tips', 'difficulty', 'est_time', 'caution', 'fees',
])
def test_design_fields_survive_lean_output(merged: dict, key: str) -> None:
    """design 内层字段同样不能丢 —— 以前模型会输出它们，接入方可能直接读。"""
    assert (merged.get('design') or {}).get(key) not in (None, '', [], {}), f'design.{key} 丢了'


def test_flower_language_filled_from_knowledge(merged: dict) -> None:
    """花语改由知识库补（模型不再写）。

    花语是知识库静态属性，让模型写既费 token 又可能编造；
    但 demo 方案卡会在主花下面展示它（demo/index.html 有 ``f.flower_language``）。
    """
    for group in ('main_flowers', 'fillers', 'foliage'):
        for f in (merged.get('design') or {}).get(group) or []:
            assert f.get('flower_language'), f'{group}.{f.get("name")} 缺花语'
            assert f.get('role'), f'{group}.{f.get("name")} 缺 role'


def test_price_still_consistent_after_lean_output(merged: dict) -> None:
    """精简输出不能破坏「明细 = 合计 = price」这条一致性。"""
    bb = merged.get('budget_breakdown') or {}
    total = sum((it.get('amount') or 0) for it in (bb.get('items') or []))
    assert total == bb.get('total_estimate') == merged.get('price')


# ── 首字延迟护栏：终结工具的参数顺序 ──
#
# 模型是**按参数顺序逐个生成**的，而流式只从 `reply` 字段开始推送。
# 实测（2026-09-20 演示环境）：
#   · respond_to_user（reply 排第一）→ 首字 3.7s
#   · show_plan_card（plans 排第一、reply 排第二）→ 首字 20~25s
# 因为模型得先写完整个卡片 JSON 才轮到 reply —— 用户盯着空屏幕 20 秒。
# ⚠️ 结论：reply 永远放在终结工具参数表的**第一位**。

TERMINAL_TOOLS = ('respond_to_user', 'show_plan_card', 'show_options')


@pytest.mark.parametrize('tool_name', TERMINAL_TOOLS)
def test_terminal_tool_reply_is_first_param(tool_name: str) -> None:
    """终结工具的 `reply` 必须是第一个参数，否则首字要等几十秒。"""
    from agent.toolkit import TOOL_REGISTRY

    spec = TOOL_REGISTRY.get(tool_name)
    assert spec is not None, f'{tool_name} 未注册'
    props = list(((getattr(spec, 'parameters', None) or {}).get('properties') or {}))
    assert props, f'{tool_name} 没有 parameters.properties'
    assert props[0] == 'reply', (
        f'{tool_name} 的第一个参数是 {props[0]!r}，应为 reply。'
        '模型按参数顺序生成，非 reply 字段排在前面会让用户多等几十秒（见上方实测）。\n'
        f'当前顺序：{props}'
    )


@pytest.mark.parametrize('tool_name', TERMINAL_TOOLS)
def test_terminal_tool_reply_is_required(tool_name: str) -> None:
    """`reply` 须为必填 —— 否则模型可能跳过它，卡片就没有文字说明。"""
    from agent.toolkit import TOOL_REGISTRY

    spec = TOOL_REGISTRY.get(tool_name)
    required = ((getattr(spec, 'parameters', None) or {}).get('required') or [])
    assert 'reply' in required, f'{tool_name} 的 required 缺 reply：{required}'
