"""mapper.py —— 平台库结构 → 业务实体映射草案生成（纯函数，不连库）。

输入是 ``platform_db_discover`` 返回的脱敏 schema profile（tables / columns /
foreign_keys），本模块按表名 / 字段名启发式为 plan / shop / order / user 生成
候选映射草案，供人工审核后经 ``platform_mapping_save_draft`` 落库、审核激活。

安全边界：
- 本模块不访问任何数据库，也不会写入目标平台库；
- 只产出 draft 数据，激活 / 撤销由 mapping_store 的状态机控制。

历史（2026-09 重构）：早期版本基于“智能体本地 DATABASE_URL”做 auto_* 查询 / 下单
适配（infer_mapping / auto_query / auto_search_plans / auto_search_shops /
auto_create_order / auto_list_orders 及 data_mapping.json 人工写库配置）。该“本地
商品/订单镜像”方案会误导 LLM 读到空表，已整体废弃并移除，相关模块 gateway.py 已删除。
"""
from __future__ import annotations

import re
from typing import Any

_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MAX_TABLES = 200

CANONICAL_ENTITIES: dict[str, dict[str, Any]] = {
    'plan': {
        'table_hints': ['plan', 'plans', 'product', 'products', 'goods', 'flower', 'item', 'commodity'],
        'columns': {
            'id': ['id', 'plan_id', 'product_id', 'sku', 'goods_id'],
            'name': ['name', 'title', 'product_name', 'plan_name', 'goods_name'],
            'price': ['price', 'amount', 'sale_price', 'selling_price', 'final_price'],
            'desc': ['desc', 'description', 'detail', 'intro', 'summary'],
            'image': ['image', 'img', 'effect_image_url', 'cover', 'photo', 'picture'],
            'merchant': ['merchant', 'merchant_name', 'shop_name', 'store_name'],
            'tags': ['tags', 'tag', 'labels', 'label'],
            'shop_id': ['shop_id', 'store_id', 'merchant_id'],
        },
    },
    'shop': {
        'table_hints': ['shop', 'shops', 'store', 'stores', 'merchant', 'vendor', 'supplier'],
        'columns': {
            'id': ['id', 'shop_id', 'store_id', 'merchant_id'],
            'name': ['name', 'shop_name', 'store_name', 'merchant_name', 'title'],
            'rating': ['rating', 'score', 'star', 'stars'],
            'distance_km': ['distance_km', 'distance', 'dist', 'km'],
            'price_range': ['price_range', 'range', 'price_range_text'],
            'lat': ['lat', 'latitude'],
            'lng': ['lng', 'longitude', 'lon'],
        },
    },
    'order': {
        'table_hints': ['order', 'orders', 'trade', 'purchase'],
        'columns': {
            'id': ['id', 'order_id', 'trade_id', 'order_no', 'no'],
            'user_id': ['user_id', 'customer_id', 'buyer_id', 'uid'],
            'plan_id': ['plan_id', 'product_id', 'item_id', 'goods_id'],
            'total_price': ['total_price', 'total', 'amount', 'pay_amount', 'total_amount'],
            'status': ['status', 'state', 'order_status'],
            'created_at': ['created_at', 'create_time', 'created_time', 'order_time', 'pay_time'],
        },
    },
    'user': {
        'table_hints': ['user', 'users', 'member', 'customer', 'account'],
        'columns': {
            'id': ['id', 'user_id', 'uid', 'member_id', 'customer_id'],
            'name': ['name', 'nickname', 'nick_name', 'username', 'display_name'],
            'phone': ['phone', 'mobile', 'tel', 'phone_number'],
            'role': ['role', 'user_role', 'type'],
        },
    },
}


def tool_result(ok: bool, data: Any = None, error: str | None = None) -> str:
    """把工具执行结果统一封装成 JSON 字符串（ok / data / error 三键恒定）。"""
    import json
    return json.dumps({'ok': ok, 'data': data, 'error': error}, ensure_ascii=False)


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _score_table(table: str, hints: list[str]) -> int:
    n = _norm(table)
    score = 0
    for hint in hints:
        hn = _norm(hint)
        if hn == n:
            score += 10
        elif hn in n or n in hn:
            score += 5
        elif any(part == hn for part in re.split(r'[_\-]', table.lower())):
            score += 3
    return score


def _pick_column(columns: list[dict[str, Any]], candidates: list[str]) -> str | None:
    for c in columns:
        cname = c.get('name', '')
        if any(_norm(cname) == _norm(cand) for cand in candidates):
            return cname
    for c in columns:
        cname = c.get('name', '')
        for cand in candidates:
            if len(cand) >= 4 and len(_norm(cname)) >= 3 and (_norm(cand) in _norm(cname) or _norm(cname) in _norm(cand)):
                return cname
    return None


def generate_mapping_draft(profile: dict[str, Any]) -> dict[str, Any]:
    """根据已净化的 schema profile 生成映射草案；永远不会激活或写入目标库。"""
    tables = profile.get('tables') if isinstance(profile, dict) else None
    if not isinstance(tables, list):
        raise ValueError('profile.tables must be a list')
    draft: dict[str, Any] = {
        'status': 'draft',
        'source_id': profile.get('source_id', ''),
        'dialect': profile.get('dialect', 'unknown'),
        'schema_fingerprint': profile.get('schema_fingerprint', ''),
        'entities': {},
        'requires_human_review': [],
    }
    for entity_name, entity_def in CANONICAL_ENTITIES.items():
        candidates: list[dict[str, Any]] = []
        for item in tables[:_MAX_TABLES]:
            if not isinstance(item, dict):
                continue
            table = str(item.get('table', ''))
            columns = item.get('columns', [])
            if not _IDENTIFIER.match(table) or not isinstance(columns, list):
                continue
            table_score = _score_table(table, entity_def['table_hints'])
            column_map: dict[str, str] = {}
            evidence: list[dict[str, Any]] = []
            risks: list[str] = []
            for canonical, names in entity_def['columns'].items():
                col = _pick_column(columns, names)
                if col:
                    column_map[canonical] = col
                    meta = next((c for c in columns if isinstance(c, dict) and c.get('name') == col), {})
                    evidence.append({'canonical': canonical, 'column': col, 'reason': 'name_match', 'type': meta.get('type', '')})
                    if canonical in {'price', 'total_price', 'status'}:
                        risks.append(f'{canonical} requires semantic and unit verification')
            score = round((table_score / 10.0) * 0.35 + (len(column_map) / max(1, len(entity_def['columns']))) * 0.65, 2)
            if score > 0:
                if score < 0.75:
                    risks.append('low confidence or incomplete fields')
                if not item.get('foreign_keys') and entity_name in {'order', 'plan'}:
                    risks.append('relationship evidence is missing')
                candidates.append({'table': table, 'columns': column_map, 'confidence': min(score, 1.0), 'evidence': evidence, 'risks': risks})
        candidates.sort(key=lambda x: x['confidence'], reverse=True)
        selected = candidates[0] if candidates else {'table': None, 'columns': {}, 'confidence': 0, 'evidence': [], 'risks': ['no candidate found']}
        if selected['confidence'] < 0.85:
            draft['requires_human_review'].append({'entity': entity_name, 'reason': 'confidence below approval threshold'})
        draft['entities'][entity_name] = {'selected': selected, 'alternatives': candidates[1:3]}
    return draft
