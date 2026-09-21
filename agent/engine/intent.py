"""轻量业务意图路由：只负责明显意图，模糊消息交给完整 ReAct。"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentRoute:
    name: str
    max_iterations: int
    tools: frozenset[str] | None = None


_CARE = frozenset({'retrieve_knowledge', 'respond_to_user', 'search_history', 'get_user_profile'})
_GREETING = frozenset({'suggest_greetings', 'render_greeting_card', 'respond_to_user', 'show_options'})
_IMAGE = frozenset({'generate_effect_image', 'respond_to_user'})


def classify(message: str) -> IntentRoute | None:
    text = str(message or '').strip()
    if not text:
        return None
    if any(w in text for w in ('方案', '定制', 'DIY', '自己配', '自己搭')) and any(w in text for w in ('效果图', '出图', '生成图')):
        return IntentRoute('image', 2, _IMAGE)
    if any(w in text for w in ('效果图', '出图', '生成图', '看看成品')) and any(w in text for w in ('这个方案', '此方案', '该方案', '给「', '给“')):
        return IntentRoute('image', 2, _IMAGE)
    if any(w in text for w in ('祝福', '贺卡', '寄语')) and not any(w in text for w in ('买', '推荐', '预算')):
        return IntentRoute('greeting', 3, _GREETING)
    stripped = re.sub(r'(?:不要|不用|无需)(?:方案和图片|方案和效果图|方案|图片|效果图)', '', text)
    if len(stripped) <= 120 and any(w in stripped for w in ('怎么养', '如何养', '养护', '换水', '剪根', '保鲜', '醒花')) and not any(w in stripped for w in ('推荐', '买', '送', '预算', '方案', '店', '价格')):
        return IntentRoute('qa', 2, _CARE)
    return None
