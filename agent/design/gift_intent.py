"""送花心意：用户想让收花人感受到的内容，不是用户当前的情绪或花束风格。"""
from __future__ import annotations

import re
from typing import Any

from agent.knowledge import query_knowledge

# 与花语不是一套枚举：一句「想认真道歉」不能被硬压成某个花语词。
INTENT_LANGUAGE = {
    'gratitude': ('感恩', '感激', '尊敬', '温馨', '用心'),
    'apology': ('真挚', '温柔', '不变的心', '守护'),
    'affection': ('爱情', '浪漫', '告白', '暗恋', '温柔'),
    'comfort': ('陪伴', '守护', '希望', '温柔的力量', '关怀'),
    'encouragement': ('希望', '坚强', '阳光', '勇敢', '逆境中的力量'),
    'celebration': ('喜悦', '快乐', '胜利', '幸福', '圆满'),
    'remembrance': ('怀念', '思念', '永恒的记忆', '哀思'),
}
_INTENT_PATTERNS = {
    'gratitude': r'感谢|谢谢|感激|辛苦了|付出',
    'apology': r'道歉|对不起|认错|赔不是|求原谅|和好|重新开始',
    'affection': r'表白|告白|喜欢[你她他]|爱[你她他]|心意|暗恋|求婚',
    'comfort': r'安慰|陪伴|难过|低落|失落|不开心|生病|住院',
    'encouragement': r'打气|鼓励|加油|振作|撑过去|压力大',
    'celebration': r'恭喜|祝贺|庆祝|升职|毕业|乔迁',
    'remembrance': r'怀念|思念|想念|悼念|祭奠',
}
_MESSAGE_RE = re.compile(r'(?:想|希望|要)(?:借(?:这束)?花|用(?:这束)?花)?(?:向|跟|对)?[^，。；;]{0,12}?(?:表达|传达|告诉|说)(?:给|向|跟|对)?(?:她|他|妈妈|母亲|朋友|女友|男友)?([^，。；;]{2,70})')


def understand_gift_message(text: str) -> tuple[str | None, str | None]:
    """保守提取；否定表达不当成肯定需求，原话只截取明确的表达片段。"""
    text = text or ''
    segment = _MESSAGE_RE.search(text)
    message = segment.group(1).strip() if segment else None
    for intent, pattern in _INTENT_PATTERNS.items():
        for match in re.finditer(pattern, text):
            prefix = text[max(0, match.start() - 4):match.start()]
            if re.search(r'(?:不|别|无需|不用|并非|不是)\s*(?:想|要|用|去)?\s*$', prefix):
                continue
            return intent, message
    return None, message


def retracts_gift_intent(text: str, intent: str | None) -> bool:
    """用户明确否定上一轮心意时可撤销旧推断。"""
    pattern = _INTENT_PATTERNS.get(intent or '')
    if not pattern:
        return False
    return bool(re.search(r'(?:不|别|无需|不用|并非|不是)\s*(?:想|要|用|去)?\s*(?:' + pattern + r')', text))


def rank_products_for_gift(rows: list[dict[str, Any]], intent: str | None,
                           budget: float | None = None) -> list[dict[str, Any]]:
    """只按商品确实写出的花材软排序；未知成分保持顺序，绝不伪称匹配。"""
    if intent not in INTENT_LANGUAGE or len(rows) < 2:
        return rows
    flowers = query_knowledge('flower', '')['results']
    desired = set(INTENT_LANGUAGE[intent])

    def score(row: dict[str, Any]) -> int:
        # 仅依据业务侧已经返回的文字，不能从图片或凭印象猜花材。
        text = ' '.join(str(row.get(k) or '') for k in ('name', 'description', 'flowers', 'materials'))
        return max((len(desired.intersection(f.get('flower_language') or []))
                    for f in flowers if f['name'] in text), default=0)

    def priority(row: dict[str, Any]) -> tuple[int, int]:
        # 仅 price_yuan 是已知统一单位；其他平台字段可能以分计价，不猜。
        price = row.get('price_yuan')
        over_budget = int(budget is not None and isinstance(price, (int, float))
                          and not isinstance(price, bool) and price > budget)
        return over_budget, -score(row)

    return sorted(rows, key=priority)
