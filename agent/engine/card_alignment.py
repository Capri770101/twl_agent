"""让最终回复点名的商品、卡片列表和自报数量保持一致。"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from agent.engine.ui_protocol import UIType

logger = logging.getLogger('agent')
_REPLY_PLAN_COUNT_RE = re.compile(r'(\d+)\s*(?:款|束)')


def _reply_declared_count(reply: str) -> int:
    """只识别回复里首次提到的商品数量。"""
    match = _REPLY_PLAN_COUNT_RE.search(reply or '')
    if not match:
        return 0
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return 0


def _align_card_data_with_reply(ui: UIType, data: dict, reply: str, *,
                                name_mentioned: Callable[[str, str], bool],
                                align_products: Callable[[list[Any], str], list[Any]]) -> dict:
    """商品卡服从最终回复点名，混合卡不加入没提及的商品。"""
    if ui != UIType.PLAN_CARD or not isinstance(data, dict):
        return data
    plans = data.get('plans')
    if not isinstance(plans, list) or not plans:
        return data
    diy = [p for p in plans if isinstance(p, dict) and (p.get('diy') is True or p.get('design'))]
    prods = [p for p in plans if isinstance(p, dict) and not (p.get('diy') is True or p.get('design'))]
    if not prods:
        return data
    named = [p for p in prods if name_mentioned(str(p.get('name') or ''), reply or '')]
    if diy and not named:
        logger.info('[agent] 混合卡：文案未点名任何商品 → 只保留 DIY 方案（去掉 %d 款商品）', len(prods))
        return {**data, 'plans': diy}
    aligned = align_products(prods, reply)
    if not aligned or aligned == prods:
        logger.info('[agent] 商品卡对齐无变化：%d 款（reply %d 字，点名命中 %d 款）',
                    len(prods), len(reply or ''), len(named))
        return data
    logger.info('[agent] 商品卡已与回复对齐：%d 款 → %d 款（reply %d 字）',
                len(prods), len(aligned), len(reply or ''))
    out = dict(data)
    out['plans'] = diy + aligned
    return out


def _collect_plan_rows(tool_log: list[Any]) -> list[dict[str, Any]]:
    """收集本轮所有成功的商品查询结果（不只取最后一次）。"""
    rows: list[dict[str, Any]] = []
    for tc in tool_log or []:
        if getattr(tc, 'name', '') != 'platform_db_query_entity':
            continue
        if getattr(tc, 'status', '') != 'ok':
            continue
        args = getattr(tc, 'arguments', None) or {}
        if not isinstance(args, dict) or args.get('entity') != 'plan':
            continue
        raw = getattr(tc, 'result', None)
        try:
            result = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(result, dict) or result.get('ok') is not True:
            continue
        data = result.get('data')
        if isinstance(data, list):
            rows.extend(r for r in data if isinstance(r, dict))
    return rows


def _align_reply_count_with_card(reply: str, ui: UIType, data: dict[str, Any]) -> str:
    """用实际商品卡数量替换回复中第一处「N 款/束」声明。"""
    if ui != UIType.PLAN_CARD or not isinstance(data, dict) or not reply:
        return reply
    plans = data.get('plans')
    if not isinstance(plans, list):
        return reply
    prods = [p for p in plans if isinstance(p, dict) and not (p.get('diy') is True or p.get('design'))]
    if not prods:
        return reply
    declared = _reply_declared_count(reply)
    if declared <= 0 or declared == len(prods):
        return reply
    new_reply, n = _REPLY_PLAN_COUNT_RE.subn(
        lambda m: f'{len(prods)}{m.group(0)[len(m.group(1)):]}', reply, count=1)
    if n != 1:
        return reply
    logger.info('[agent] 文案自报 %d 款 ≠ 卡片 %d 款 → 已回写数字', declared, len(prods))
    return new_reply
