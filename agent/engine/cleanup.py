"""有序回复清理流水线；顺序受 test_cleanup_pipeline 保护。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.engine.card_summary import _ensure_card_summary, _ensure_non_empty_reply
from agent.safety.formatter import _strip_separator_lines, _strip_internal_leak, _neutralize_dangerous_tags
from agent.safety.image_claim import _finalize_image_claim
from agent.safety.reasoning import _finalize_reasoning_leak
from agent.safety.trade_claim import _finalize_trade_claim
from agent.engine.ui_protocol import UIType


@dataclass(frozen=True)
class _CleanupStep:
    """清理链的一步（顺序即语义）。"""

    name: str
    fn: Callable[..., str]
    why: str
    needs: tuple[str, ...] = ('reply',)
    enabled: Callable[[], bool] | None = None


def build_cleanup_pipeline(demo_trade_note: Callable[[str], str],
                           shop_entity_enabled: Callable[[], bool]) -> tuple[_CleanupStep, ...]:
    """显式注入部署能力以避免循环导入 agent.agent。"""
    return (
        _CleanupStep('strip_separator_lines', _strip_separator_lines,
                     '先删独立分隔线（--- / ***）：放最前，避免它被后续步骤当成正文处理'),
        _CleanupStep('strip_internal_leak', _strip_internal_leak,
                     '隐私兜底：清掉工具名 / 内部字段 / 原始数据行。'
                     '**必须早于 XSS 转义** —— 否则转义后的实体会让这里的模式匹配失效'),
        _CleanupStep('neutralize_dangerous_tags', _neutralize_dangerous_tags,
                     'XSS 兜底：reply 虽是纯文本，但不能假设接入端会转义（审计 P0）。'
                     '放在 _strip_internal_leak 之后，避免转义产物被再删一遍'),
        _CleanupStep('ensure_card_summary', _ensure_card_summary,
                     '要点兜底：把卡片数据补成一句可读要点。依赖**已清理过的**正文与卡片数据，'
                     '放这里才能读到干净文本', needs=('reply', 'ui', 'data')),
        _CleanupStep('ensure_non_empty_reply', _ensure_non_empty_reply,
                     '⚠️ 非空兜底：脱敏可能把正文整段清空 → 用户看到空气泡。必须紧跟清理类步骤',
                     needs=('reply', 'ui')),
        _CleanupStep('demo_trade_note', demo_trade_note,
                     '体验版（不承接下单）边界说明：仅在非交易部署下追加',
                     enabled=lambda: not shop_entity_enabled()),
        _CleanupStep('finalize_image_claim', _finalize_image_claim,
                     '生图声明兜底：护栏纠正后仍谎称已出图 → 整段换成如实说明'
                     '（不能让用户去找一张不存在的图）', needs=('reply', 'tool_log')),
        _CleanupStep('finalize_trade_claim', _finalize_trade_claim,
                     '交易声称兜底：护栏纠正后仍谎称已下单/支付 → 整段换成如实说明'
                     '（不能让用户去找一张不存在的订单）', needs=('reply', 'tool_log')),
        _CleanupStep('finalize_reasoning_leak', _finalize_reasoning_leak,
                     '独白兜底：纠正后仍是「我该怎么回答」→ 整段换成面向用户的话。'
                     '⚠️ 必须放最后：它是**整段替换**，放前面会把后面步骤的产出一起覆盖掉',
                     needs=('reply', 'ui')),
    )


def run_cleanup_pipeline(reply: str, *, pipeline: tuple[_CleanupStep, ...],
                         ui: UIType, data: dict[str, Any], tool_log: list[Any]) -> str:
    """按命名步骤执行；首参数用位置方式兼容 text/reply 两种历史命名。"""
    for step in pipeline:
        if step.enabled is not None and not step.enabled():
            continue
        kwargs: dict[str, Any] = {}
        if 'ui' in step.needs:
            kwargs['ui'] = ui
        if 'data' in step.needs:
            kwargs['data'] = data
        if 'tool_log' in step.needs:
            kwargs['tool_log'] = tool_log
        reply = step.fn(reply, **kwargs)
    return reply
