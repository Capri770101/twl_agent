"""内部独白检测、流式过程说明筛查与最终回复兜底。"""
from __future__ import annotations

import logging
import re
from typing import Any

from agent.tools_meta import CARD_UIS as _CARD_UIS

logger = logging.getLogger('agent')

# 线上实测：寒暄时模型曾把「用户说…我应该调用工具…」等内部独白当作
# 对用户的回复。强信号命中一条即拦截；元叙述至少两条才拦截，避免误伤。
_REASONING_TOOL_NAMES = (
    'respond_to_user', 'show_plan_card', 'show_options', 'generate_diy_plan',
    'revise_diy_plan', 'generate_effect_image', 'retrieve_knowledge',
    'platform_db_query_entity', 'search_history', 'get_user_profile',
    'save_user_profile', 'save_memory', 'suggest_greetings', 'render_greeting_card',
)
_REASONING_PAYLOAD_LEAK = re.compile(
    r'[{[]\s*"(?:reply|ui|data|stage|intent)"|"(?:reply|ui|data|stage|intent)"\s*:'
)
_REASONING_META_MARKERS = (
    '用户说', '用户问', '用户想', '用户希望', '用户表示', '用户的意思是',
    '我需要判断', '判断意图', '意图：', '我应该用', '我应该调用', '应该调用',
    '不需要调用任何工具', '不属于任何意图', 'chitchat',
)
_REASONING_FALLBACK_TEXT = '抱歉，刚才没说清楚 😅 想聊花、想要方案或者问养护，随时跟我讲～'
_REASONING_FALLBACK_CARD = '方案已经配好了，具体细节都在下面的卡片里，想调整哪部分随时跟我说～'


def _looks_like_reasoning_leak(reply: str) -> bool:
    """识别工具名、工具参数 JSON 残片或至少两项内部元叙述。"""
    text = reply or ''
    if not text:
        return False
    if any(t in text for t in _REASONING_TOOL_NAMES):
        return True
    if _REASONING_PAYLOAD_LEAK.search(text):
        return True
    return sum(1 for m in _REASONING_META_MARKERS if m in text) >= 2


# 流式回复边收边推，一旦泄漏无法回滚。粗筛故意更宽松：误判只导致
# 暂缓推送，最终 reply 仍通过清理链；漏判则会直接暴露内部过程。
_PROCESS_NARRATION_MARKERS = (
    '用户说', '用户问', '用户想', '用户希望', '用户表示',
    '我注意到', '我意识到', '我需要判断', '我需要先', '我需要确认',
    '我应该', '应该调用', '判断意图', '意图：', '让我先', '我先查',
    'chitchat', '不属于任何意图',
)


def _has_process_narration(text: str) -> bool:
    """流式文字中是否出现过程说明特征。"""
    return any(m in (text or '') for m in _PROCESS_NARRATION_MARKERS)


def _needs_reasoning_nudge(reply: str) -> bool:
    """本轮是否应拦下内部独白并要求重答。"""
    return _looks_like_reasoning_leak(reply)


def _reasoning_nudge_text() -> str:
    """只注入本轮上下文、不落库的纠正指令。"""
    return (
        '[系统校验未通过] 你刚才发出去的内容**不是给用户看的回复**，而是你自己在分析该怎么做'
        '——里面出现了「用户说…」「我需要判断意图」「我应该调用某个工具」这类内部推理，'
        '甚至带出了工具名和参数格式。用户看到会以为你在自言自语，这是严重的体验缺陷。\n'
        '现在请重新给出**真正面向用户**的那句话：\n'
        '· 只写用户该看到的内容（回答 / 追问 / 说明），保持自然口语；\n'
        '· 绝不写分析过程、意图判断、工具名、参数或任何 JSON。'
    )


def _finalize_reasoning_leak(reply: str, ui: Any) -> str:
    """模型重答仍泄漏时，确定性地替换成面向用户的文案。"""
    if not _looks_like_reasoning_leak(reply):
        return reply
    logger.warning('[agent] 回复仍是内部独白，已替换为面向用户的兜底文案')
    return _REASONING_FALLBACK_CARD if ui in _CARD_UIS else _REASONING_FALLBACK_TEXT
