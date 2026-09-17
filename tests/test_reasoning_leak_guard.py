"""「内部独白泄漏」护栏的回归测试。

背景（2026-09-17 线上实测，演示实例）：寒暄类消息会返回模型的内部独白 ——
「用户说"谢谢你"… 我应该用 respond_to_user 工具，纯文字回复」+ 工具参数残片 {"reply"。
ui 判定是 text（看起来"通过"），但用户直接看到推理过程和工具名。
10 条测试用例里复现 2 次，低频但真实。

这里锁住三件事：
1. 真实泄漏样本必须被判出来（含工具名 / JSON 残片 / 元叙述 ≥2 条三条路径）；
2. **正常回复不能被误伤**（尤其「你刚才说…」「我建议」这类近义表达）；
3. 纠正一次仍泄漏 → 确定性整段替换。
"""

from __future__ import annotations

from agent.agent import (
    UIType,
    _finalize_reasoning_leak,
    _looks_like_reasoning_leak,
    _needs_reasoning_nudge,
    _reasoning_nudge_text,
)

# ── 1. 真实泄漏样本 ───────────────────────────────────────────────────────

# 线上抓到的原样样本（2026-09-17，演示实例，消息「谢谢你」）
REAL_LEAK_1 = (
    '用户说"谢谢你"，这是一个感谢/告别的话语。我需要判断意图：\n'
    '- 这不是购买意图，不是问知识，不是要DIY设计\n'
    '- 这是闲聊/寒暄类的回应，属于 chitchat 或 other\n'
    '我应该用 respond_to_user 工具，纯文字回复'
)
# 第二次复现（同一场景，还带出工具参数残片）
REAL_LEAK_2 = (
    '用户说了"谢谢你"，这是一个简单的感谢/告别消息。我应该用温暖友好的语气回应，'
    '不需要调用任何工具。这是纯闲聊场景，用 respond_to_user 以 text 类型回复即可。'
    '不推荐商品、不出方案卡、不催单。保持简短亲切。{"reply"'
)


def test_real_leak_samples_detected() -> None:
    assert _looks_like_reasoning_leak(REAL_LEAK_1)
    assert _looks_like_reasoning_leak(REAL_LEAK_2)
    assert _needs_reasoning_nudge(REAL_LEAK_1)
    assert _needs_reasoning_nudge(REAL_LEAK_2)


def test_tool_name_alone_is_enough() -> None:
    """强信号：工具名绝不该出现在给用户的话里，命中一条即判定。"""
    assert _looks_like_reasoning_leak('我用 generate_diy_plan 帮你配好了')
    assert _looks_like_reasoning_leak('调用 platform_db_query_entity 查一下')
    assert _looks_like_reasoning_leak('接下来 show_options')


def test_tool_payload_fragment_is_enough() -> None:
    """强信号：工具参数的 JSON 残片。"""
    assert _looks_like_reasoning_leak('好的，这就来 {"reply"')
    assert _looks_like_reasoning_leak('{"ui": "plan_card", "data": {}}')
    assert _looks_like_reasoning_leak('"intent": "design"')


def test_meta_narration_needs_two_markers() -> None:
    """元叙述单条可能是巧合，两条才算泄漏 —— 避免误伤。"""
    assert _looks_like_reasoning_leak('用户说想要送妈妈，我需要判断意图')
    assert _looks_like_reasoning_leak('判断意图：这是 chitchat')
    # 只命中一条 → 不判（下面的正常回复用例也覆盖这点）
    assert not _looks_like_reasoning_leak('你说的这句话我懂')


# ── 2. 不误伤正常回复 ─────────────────────────────────────────────────────

NORMAL_REPLIES = [
    '你好呀，我是你的花艺小助手 🌸 想设计花束、看现成款式或者问养护，都可以跟我说。',
    '抱抱你，心情不好的时候不用勉强自己说原因。如果想给自己插一小束花，向日葵配尤加利挺提气。',
    '不客气呀～能帮到你很开心，之后想配花、改方案或者要效果图，随时来找我。',
    '给你挑了一束暖阳感的：向日葵 4 支 + 非洲菊 3 支，预算大约 150 元，配牛皮纸包装。',
    '玫瑰代表热烈的爱，康乃馨更适合表达对长辈的感恩，这两者花语侧重不同。',
    '你刚才说的预算 300 我记下了，这次按粉色系重新配了一版。',
    '想看一下这束花怎么养得久？建议每天斜剪花脚 2cm，换水时加半包保鲜剂。',
    '刚才那句话我说得不清楚，重新讲一遍：这束花的主花是洋桔梗，配了满天星点缀。',
    '我建议先确认一下送花对象和场合，这样配出来的花更贴合心意。',
]


def test_normal_replies_not_flagged() -> None:
    for text in NORMAL_REPLIES:
        assert not _looks_like_reasoning_leak(text), f'误伤: {text}'


def test_empty_and_none_safe() -> None:
    assert not _looks_like_reasoning_leak('')
    assert not _looks_like_reasoning_leak(None)  # type: ignore[arg-type]


# ── 3. 确定性兜底 ─────────────────────────────────────────────────────────

def test_finalize_replaces_text_leak() -> None:
    out = _finalize_reasoning_leak(REAL_LEAK_1, UIType.TEXT)
    assert not _looks_like_reasoning_leak(out)
    assert 'respond_to_user' not in out
    assert out.strip()


def test_finalize_replaces_card_leak_with_card_wording() -> None:
    """卡片场景的兜底文案要指向卡片，不能是一句莫名其妙的道歉。"""
    out = _finalize_reasoning_leak(REAL_LEAK_2, UIType.PLAN_CARD)
    assert not _looks_like_reasoning_leak(out)
    assert '卡片' in out


def test_finalize_keeps_normal_reply_untouched() -> None:
    normal = '不客气呀～能帮到你很开心，之后想配花随时找我。'
    assert _finalize_reasoning_leak(normal, UIType.TEXT) == normal


# ── 4. 纠正文案本身的质量 ─────────────────────────────────────────────────

def test_nudge_text_forbids_reasoning_and_names() -> None:
    txt = _reasoning_nudge_text()
    assert '不是给用户看的回复' in txt
    assert '工具名' in txt and 'JSON' in txt
    # 纠正文案是发给模型的，提到工具名没问题，但要说清"用户不该看到"
    assert '绝不写' in txt
