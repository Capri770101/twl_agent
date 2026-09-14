"""L3「澄清式追问」回归门：信息不足时不硬编方案。

设计要点（这三条就是本文件的断言主线）：
1. **只在 DIY 路径上追问**——平台在售推荐是另一条路，追问反而多事；
2. **只问 recipient / occasion / budget**，且一次最多 2 个（不做表单式盘问）；
3. **只问一次 + 「随便/你决定」豁免**——追问是兜底，不能让用户觉得被审问。

历史动机：prompt 里早有「先问清楚再设计」的口头规则，但模型遵守不稳定，
会出现「缺场合/预算也硬出一条猜的方案卡」；这里用确定性护栏兜住。
"""

from __future__ import annotations

from pathlib import Path

from agent.agent import UIType, _append_clarify, _clarify_slots
from agent.toolkit import visible_tool_specs
from agent.tools import normalize_missing_slots, respond_to_user

ROOT = Path(__file__).resolve().parents[1]
CARD = UIType.PLAN_CARD


def _slots(message: str = '帮我设计一束花', missing=None, ui=CARD, diy=True, asked=False) -> list[str]:
    return _clarify_slots(message, {'missing': missing} if missing is not None else {}, ui, diy, asked)


# ── 1. 触发条件：只有「DIY 出方案 + 模型自报缺信息」才追问 ──

def test_clarify_requires_plan_card_and_diy() -> None:
    assert _slots(missing=['recipient']) != []
    # 非方案卡（纯文本回复 / 店铺卡）不拦
    assert _slots(missing=['recipient'], ui=UIType.TEXT) == []
    # 平台在售推荐（没有 DIY 工具产出）不拦
    assert _slots(missing=['recipient'], diy=False) == []
    # 同一需求内不重复追问
    assert _slots(missing=['recipient'], asked=True) == []


def test_clarify_only_for_top3_slots() -> None:
    assert _slots(missing=['recipient', 'budget']) == ['recipient', 'budget']
    # style / colors 缺失时模型可以主动推荐，不打断用户
    assert _slots(missing=['style']) == []
    assert _slots(missing=['colors', 'style']) == []
    # 最多问 2 个（按优先级），不做表单式盘问
    assert _slots(missing=['budget', 'occasion', 'recipient']) == ['recipient', 'occasion']


def test_clarify_ignores_garbage_and_missing_field() -> None:
    assert _slots(missing=[]) == []
    assert _slots(missing=None) == []
    assert _slots(missing=['预算', 'unknown']) == []   # 中文名/编造槽位一律不认
    assert _clarify_slots('帮我设计', None, CARD, True, False) == []
    assert _clarify_slots('帮我设计', {'missing': 'recipient'}, CARD, True, False) == []


def test_clarify_exempt_when_user_lets_agent_decide() -> None:
    for msg in ('随便', '你决定就好', '都行', '直接来一束', '预算你看着办', '不看预算，你推荐'):
        assert _slots(message=msg, missing=['recipient', 'budget']) == [], f'「{msg}」被误判为需要追问'


# ── 2. 追问文案：追加而非替换（H-1 教训）──

def test_append_clarify_keeps_original_reply() -> None:
    out = _append_clarify('粉色玫瑰很百搭～', ['recipient'])
    assert out.startswith('粉色玫瑰很百搭～')          # 原回复保留在前
    assert '送给谁' in out and '想先确认一下' in out    # 追问追加在后


def test_append_clarify_handles_empty_reply_and_slots() -> None:
    out = _append_clarify('', ['occasion', 'budget'])
    assert out.startswith('在给你定方案之前')
    assert '什么场合' in out and '预算' in out
    assert _append_clarify('就这样吧', []) == '就这样吧'
    assert _append_clarify('就这样吧', ['style']) == '就这样吧'   # 非追问槽位不生成文案


# ── 3. 工具侧归一（防止模型写中文字段名 / 编造槽位）──

def test_normalize_missing_slots() -> None:
    assert normalize_missing_slots(None) == []
    assert normalize_missing_slots('recipient') == []
    assert normalize_missing_slots(['recipient', ' BUDGET ', 'x']) == ['recipient', 'budget']
    assert normalize_missing_slots(['budget', 'budget']) == ['budget']      # 去重
    # 最多保留 3 个（超出截断）
    assert normalize_missing_slots(['recipient', 'occasion', 'budget', 'style']) == ['recipient', 'occasion', 'budget']


def test_respond_to_user_exposes_and_normalizes_missing() -> None:
    out = respond_to_user(reply='好的')
    assert out['missing'] == []
    assert respond_to_user(reply='好的', missing=['预算', 'recipient'])['missing'] == ['recipient']


# ── 4. schema 与 prompt 同步（改了字段别只改一半）──

def test_schema_and_prompt_document_missing() -> None:
    for name in ('respond_to_user', 'show_plan_card'):
        spec = next(s for s in visible_tool_specs() if s.name == name)
        prop = spec.parameters['properties']['missing']
        assert prop['type'] == 'array'
        assert 'recipient' in prop['items']['enum'] and 'budget' in prop['items']['enum']
    text = (ROOT / 'agent' / 'prompts' / 'base.md').read_text(encoding='utf-8')
    assert 'missing' in text
