"""方案卡证据护栏：有实际花艺需求时防止只口述不存在的卡片。

意图判断交给模型自报的 ``intent`` 信号（respond_to_user 的结构化理解），
本模块只保留两类**事实校验**：
- 卡片是否真的由工具产出（``_card_produced``）；
- 回复是否口述了方案明细实锤（支数/价格——防「谎称有卡」）。

不用关键词猜用户意图：实测「你agent自己的原因」里的「自己」会误命中
悦己场景关键词，导致 meta 追问被强行出卡（2026-10-13 线上复现）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from agent.tools_meta import CARD_TOOLS as _CARD_TOOLS


def _card_produced(tool_log: list[Any]) -> bool:
    """本轮是否存在成功的方案/商品/店铺工具结果，而非模型口述。"""
    for tc in tool_log or []:
        if getattr(tc, 'status', '') != 'ok':
            continue
        name = getattr(tc, 'name', '')
        if name in _CARD_TOOLS:
            try:
                raw = getattr(tc, 'result', None)
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError, AttributeError):
                payload = None
            if isinstance(payload, dict) and (payload.get('ok') is False or payload.get('error') or payload.get('validation_status') == 'blocked'):
                continue
            return True
        if name == 'platform_db_query_entity':
            ent = str((getattr(tc, 'arguments', None) or {}).get('entity') or '')
            if ent in ('plan', 'shop'):
                try:
                    result = json.loads(tc.result) if isinstance(tc.result, str) else tc.result
                except (ValueError, TypeError):
                    result = None
                if isinstance(result, dict) and result.get('ok') is True and isinstance(result.get('data'), list) and result['data']:
                    return True
    return False


def _expresses_flower_need(message: str) -> bool:
    """用既有的纯正则需求抽取器识别本轮花艺需求。"""
    text = (message or '').strip()
    if not text:
        return False
    from agent.tools import extract_requirement
    req = extract_requirement(text)
    return bool(
        req.recipient or req.occasion or req.style or req.colors
        or req.budget_num or req.stem_count or req.single_flower
    )


_PROSE_QTY_PATTERNS = (
    re.compile(r'\d{1,3}\s*[朵支枝]'),
    re.compile(r'[\u4e00-\u9fff]{2,6}\s*[×xX]\s*\d{1,3}'),
)
_PROSE_PRICE_RE = re.compile(r'\d{2,4}\s*元')


def _looks_like_plan_prose(reply: str) -> bool:
    """首轮也不能凭空口述花材数量或「方案+价格」而不出卡。"""
    text = (reply or '').strip()
    if not text:
        return False
    if any(p.search(text) for p in _PROSE_QTY_PATTERNS):
        return True
    return '方案' in text and bool(_PROSE_PRICE_RE.search(text))


def _needs_card_nudge(message: str, tool_log: list[Any], reply: str = '',
                      intent: str | None = None) -> bool:
    """仅在「模型声明了设计意图却没出卡」或「回复口述方案明细」时要求补卡。

    - 模型自报 intent 时以它为准：``design`` 且没出卡 = 真缺陷，补；
      其余意图（buying/qa/chitchat/other）一律不补——buying 的商品卡只能
      来自真实查询，查不到有专门的如实说明兜底，不能用 DIY 卡冒充。
    - 模型没报 intent（没调终结工具）时不猜意图，只看回复里有没有口述
      支数/价格的实锤——这是防「谎称有方案」的证据检测，不是意图理解。
    """
    if _card_produced(tool_log):
        return False
    # 客服工具轮产出的是订单/店铺/登录卡，不是花艺方案卡。
    if any(getattr(tc, 'name', '').startswith(('query_my_', 'contact_merchant',
                                               'cancel_my_order', 'apply_refund',
                                               'update_order_address')) for tc in tool_log):
        return False
    if intent is not None:
        return intent == 'design'
    return _looks_like_plan_prose(reply)


def _card_nudge_text() -> str:
    """只进入本轮上下文的纠正指令。"""
    return (
        '[系统校验未通过] 你刚才**只用文字描述了方案，并没有真的生成方案卡**。\n'
        '本会话此前已经有过方案（或已追问过一轮），用户这句话表达的是花艺需求，'
        '必须给出可点击的方案卡，不能只用文字描述：\n'
        '· 要定制搭配 → 调用 generate_diy_plan；要在此基础上调整 → 调用 revise_diy_plan；\n'
        '· 要平台在售的现成款 → 调用 platform_db_query_entity(source_id=..., entity="plan")；\n'
        '· 拿到工具返回后，用 show_plan_card 输出卡片。\n'
        '⚠️ reply 里的方案名 / 花材组合 / 支数 / 价格，都必须来自工具返回，'
        '严禁凭记忆或想象编造（写不存在的商品名或错价是最严重的错误）。'
    )
