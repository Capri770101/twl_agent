"""L1「对话理解」回归门：结构化信号消费。

覆盖三条不变量（不依赖 LLM / 数据库 / 网络，纯注入式断言）：
1. **信号优先**：模型给出的 confirmation / image / wants_alternative 主导判断；
2. **关键词兜底**：信号缺失或非法时，行为与 L1 改造前逐字一致（零回归）；
3. **规则否决**：涉及写库 / 花钱的动作，消息命中否定词时规则一票否决——

第 3 条是这套设计的安全底线：理解交给 LLM，但不可逆动作的最终否决权留在确定性规则手里。
历史踩坑：「这个方案不行」含「行」被判成肯定 → 误确认方案入库。
"""

from __future__ import annotations

from pathlib import Path

from agent.agent import (
    _IMAGE_SIGNALS,
    _CONFIRM_SIGNALS,
    _resolve_affirmative,
    _resolve_image,
    _signal_arg,
    _wants_alternative,
)
from agent.toolkit import visible_tool_specs
from agent.tools import respond_to_user, show_plan_card

ROOT = Path(__file__).resolve().parents[1]


# ── 1. _signal_arg：缺失 / 非法 / 'none' 一律降级为 None（绝不抛异常）──

def test_signal_arg_returns_none_for_missing_or_invalid() -> None:
    assert _signal_arg(None, 'confirmation', _CONFIRM_SIGNALS) is None
    assert _signal_arg({}, 'confirmation', _CONFIRM_SIGNALS) is None
    assert _signal_arg({'confirmation': None}, 'confirmation', _CONFIRM_SIGNALS) is None
    assert _signal_arg({'confirmation': 123}, 'confirmation', _CONFIRM_SIGNALS) is None
    assert _signal_arg({'confirmation': 'maybe'}, 'confirmation', _CONFIRM_SIGNALS) is None
    # 显式 none 与字段缺失同义：都表示「本轮没有可靠信号」→ 交回关键词兜底
    assert _signal_arg({'confirmation': 'none'}, 'confirmation', _CONFIRM_SIGNALS) is None


def test_signal_arg_normalizes_case_and_space() -> None:
    assert _signal_arg({'confirmation': 'CONFIRM'}, 'confirmation', _CONFIRM_SIGNALS) == 'confirm'
    assert _signal_arg({'image': ' Decline '}, 'image', _IMAGE_SIGNALS) == 'decline'


# ── 2. _resolve_affirmative：信号优先 / 关键词兜底 / 规则否决 ──

def test_affirmative_signal_wins() -> None:
    assert _resolve_affirmative('好', 'confirm') is True
    assert _resolve_affirmative('好', 'reject') is False
    # 模型说是确认，即便消息里没有「方案 / 好」这类字眼也应认可（L1 的核心价值）
    assert _resolve_affirmative('那就这样吧', 'confirm') is True


def test_affirmative_rule_overrides_model_hallucination() -> None:
    """规则一票否决：消息命中否定词时，模型即便判 confirm 也不认。"""
    for msg in ('这个方案不行', '不是这个', '不好看', '不想要', '算了不用了', '换一个吧'):
        assert _resolve_affirmative(msg, 'confirm') is False, f'「{msg}」被规则护栏漏过'


def test_affirmative_falls_back_to_keywords_when_signal_missing() -> None:
    # 信号缺失 → 与改造前 is_affirmative 行为一致
    assert _resolve_affirmative('好的', None) is True
    assert _resolve_affirmative('可以', None) is True
    assert _resolve_affirmative('不好', None) is False
    assert _resolve_affirmative('', None) is False


# ── 3. _resolve_image：decline 取并集（保守），want 谨慎 ──

def test_image_decline_is_union_of_signal_and_keywords() -> None:
    assert _resolve_image('不要效果图', None) == (False, True)
    assert _resolve_image('随便看看', 'decline') == (False, True)
    # 模型说想要，但用户明说不要 → 拒绝优先（保守，避免「拒了又硬塞」）
    assert _resolve_image('不要效果图', 'want') == (False, True)


def test_image_want_requires_signal_or_keyword_plus_affirmative() -> None:
    assert _resolve_image('好的', 'want') == (True, False)
    assert _resolve_image('要效果图', None) == (True, False)
    assert _resolve_image('生成吧', None) == (True, False)
    # 关键词兜底路径要求「提到图 + 肯定」，避免把普通「好的」误当生图意愿
    assert _resolve_image('好的', None) == (False, False)
    assert _resolve_image('效果图呢', None) == (False, False)


def test_image_never_want_and_decline_together() -> None:
    for msg, sig in (('不要效果图', 'want'), ('要效果图', 'decline'), ('好的', 'want'), ('别出图', None)):
        want, decline = _resolve_image(msg, sig)
        assert not (want and decline), f'「{msg}」/ {sig} 同时判为想要与拒绝'


# ── 4. _wants_alternative：关键词 ∪ 模型信号 ──

def test_wants_alternative_union() -> None:
    assert _wants_alternative(None, '换一个') is True
    assert _wants_alternative({'wants_alternative': True}, '你好') is True
    assert _wants_alternative({'wants_alternative': False}, '你好') is False
    # 非布尔值不认（模型偶发写字符串）
    assert _wants_alternative({'wants_alternative': 'true'}, '你好') is False


# ── 5. 工具契约：schema 与函数返回值都带三个信号字段 ──

def test_respond_to_user_schema_exposes_signals() -> None:
    spec = next(s for s in visible_tool_specs() if s.name == 'respond_to_user')
    props = spec.parameters['properties']
    assert {'confirmation', 'image', 'wants_alternative'} <= set(props)
    assert props['confirmation']['enum'] == ['confirm', 'reject', 'none']
    assert props['image']['enum'] == ['want', 'decline', 'none']
    assert props['wants_alternative']['type'] == 'boolean'


def test_respond_to_user_normalizes_illegal_signals() -> None:
    out = respond_to_user(reply='hi')
    assert out['confirmation'] == 'none' and out['image'] == 'none' and out['wants_alternative'] is False
    out2 = respond_to_user(reply='hi', confirmation='maybe', image='WANT', wants_alternative=1)
    assert out2['confirmation'] == 'none'   # 非法枚举降级
    assert out2['image'] == 'none'
    assert out2['wants_alternative'] is True


def test_show_plan_card_shares_same_contract() -> None:
    out = show_plan_card(plans=[{'id': 'x'}], confirmation='confirm')
    assert out['confirmation'] == 'confirm' and out['image'] == 'none'


# ── 6. prompt 与代码同步：改了信号字段，md 说明不能漏 ──

def test_prompt_documents_signals() -> None:
    text = (ROOT / 'agent' / 'prompts' / 'base.md').read_text(encoding='utf-8')
    for token in ('confirmation', 'image', 'wants_alternative'):
        assert token in text, f'base.md 未说明结构化信号 {token}'
