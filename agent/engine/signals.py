"""对话确认、图像态度与换一批的结构化信号和确定性兜底。"""
from __future__ import annotations

import re

_AFFIRMATIVE = ('好', '可以', '确认', '同意', '生成', '要', '行', '是', '看看')
_NEGATIVE = (
    '不用', '不要', '不需要', '不必', '算了', '跳过', '无需', '别', '放弃',
    '不行', '不是', '不好', '不同意', '不确认', '不想要', '不合适', '不喜欢',
    '不满意', '不考虑', '换一个', '换一批',
)
_IMAGE_DECLINE_WORDS = (
    '不要生成', '不用生成', '别生成', '不生成', '不要出图', '不用出图', '别出图',
    '取消生图', '不要效果图', '不用效果图', '别效果图', '不要预览图', '不用预览图',
)
_IMAGE_TOPIC_WORDS = ('效果图', '生图', '出图', '预览图', '生成图')
_CONFIRM_SIGNALS = ('confirm', 'reject', 'none')
_IMAGE_SIGNALS = ('want', 'decline', 'none')
_ALTERNATIVE_WORDS = ('再', '换', '别的', '预算', '有没有', '其他', '看看')


def _decline_image(message: str) -> bool:
    """拒绝要直接修饰图片，不能跨句把「不要配花并生成图」判为拒绝。"""
    text = (message or '').strip()
    if not text:
        return False
    if any(w in text for w in _IMAGE_DECLINE_WORDS):
        return True
    return bool(re.search(r'(?:不要|不用|无需|不需要|别|取消)\s*(?:再|自动|继续)?\s*(?:生成|制作|看|展示)?\s*(?:效果图|预览图|图片|图像|生图|出图|图)', text)
                or re.search(r'(?:效果图|预览图|图片|生图|出图)[，,：:\s]*(?:就|也|先)?(?:不要|不用|不需要|免了|算了|取消)', text))


def is_affirmative(text: str) -> bool:
    """否定优先于单字肯定，避免「这个方案不行」被误认为确认。"""
    t = (text or '').strip()
    if not t:
        return False
    if any(k in t for k in _NEGATIVE):
        return False
    return any(k in t for k in _AFFIRMATIVE)


def _signal_arg(respond_args: dict | None, key: str, allowed: tuple[str, ...]) -> str | None:
    """非法、缺失或 none 一律返回 None，让调用方回退到关键词判定。"""
    if not isinstance(respond_args, dict):
        return None
    raw = respond_args.get(key)
    if not isinstance(raw, str):
        return None
    val = raw.strip().lower()
    if val not in allowed or val == 'none':
        return None
    return val


def _resolve_affirmative(message: str, signal: str | None) -> bool:
    """模型明确确认优先，但否定词始终拥有否决权。"""
    text = message or ''
    if any(k in text for k in _NEGATIVE):
        return False
    if signal == 'confirm':
        return True
    if signal == 'reject':
        return False
    return is_affirmative(text)


def _img_mentioned(text: str) -> bool:
    """图像相关词只用于「想要图」的关键词兜底。"""
    t = text or ''
    return any(w in t for w in _IMAGE_TOPIC_WORDS) or '生成' in t


def _resolve_image(message: str, signal: str | None) -> tuple[bool, bool]:
    """图片意图返回 (want, decline)，明确拒绝时绝不生成。"""
    text = message or ''
    if signal == 'decline' or _decline_image(text):
        return False, True
    want = signal == 'want' or (_img_mentioned(text) and is_affirmative(text))
    return want, False


def _wants_alternative(respond_args: dict | None, message: str) -> bool:
    """关键词和模型信号取并集；清理已推方案标记是幂等操作。"""
    if any(w in (message or '') for w in _ALTERNATIVE_WORDS):
        return True
    return isinstance(respond_args, dict) and respond_args.get('wants_alternative') is True
