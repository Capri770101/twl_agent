"""DIY 澄清追问；已有信息或者用户授权自行决定时不重复提问。"""
from __future__ import annotations

from typing import Any

from agent.engine.ui_protocol import UIType

_CLARIFY_PRIORITY = ('recipient', 'occasion', 'budget')
_CLARIFY_EXEMPT = (
    '随便', '你决定', '你定', '你看着办', '看着办', '都行', '都可以', '不用问',
    '别问', '直接来', '直接出', '推荐就行', '你推荐', '帮我挑', '帮我选',
)
_SLOT_ASK = {
    'recipient': '送给谁（妈妈 / 女朋友 / 朋友 / 长辈 / 同事…）',
    'occasion': '什么场合（生日 / 纪念日 / 求婚 / 探病 / 开业 / 感谢…）',
    'budget': '预算大概多少（100 以内 / 200 左右 / 300-500 / 500 以上）',
}


def _has_any_requirement(req: Any) -> bool:
    """会话需求有至少一个有效信号。"""
    return bool(req and (req.recipient or req.occasion or req.budget_num is not None
                         or req.budget_anchor or req.scene or req.style or req.colors))


def _clarify_slots(message: str, respond_args: dict | None, ui: UIType,
                   diy_produced: bool, asked_before: bool, session_req: Any = None) -> list[str]:
    """仅对首次 DIY 的未明确收花人、场合或预算追问最多两项。"""
    if ui != UIType.PLAN_CARD or not diy_produced or asked_before:
        return []
    if any(w in (message or '') for w in _CLARIFY_EXEMPT):
        return []
    raw = respond_args.get('missing') if isinstance(respond_args, dict) else None
    reported = {str(x).strip().lower() for x in raw} if isinstance(raw, list) else set()
    slots = [s for s in _CLARIFY_PRIORITY if s in reported][:2]
    if not slots and session_req is not None and not _has_any_requirement(session_req):
        slots = ['recipient', 'occasion']
    return slots


def _append_clarify(reply: str, slots: list[str]) -> str:
    """仅在模型未追问时补上追问句。"""
    asks = '；'.join(_SLOT_ASK[s] for s in slots if s in _SLOT_ASK)
    if not asks:
        return reply
    base = (reply or '').strip()
    if base and ('？' in base or '?' in base):
        return base
    tail = f'在给你定方案之前，想先确认一下：{asks}？'
    return f'{base}\n{tail}' if base else tail
