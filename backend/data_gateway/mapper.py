"""mapper.py —— 未知数据库自动适配层（启发式字段映射）。

设计目标：
- 面对一个未知数据库，先自动发现表结构；
- 再通过表名/字段名启发式推断出“业务实体”对应的表和字段；
- 提供通用查询入口，把任意库里的数据映射成智能体认识的标准业务字段。

当前覆盖的实体：
- plan（花艺方案/商品）
- shop（店铺）
- order（订单）
- user（用户）

注意：这是“自动适配”的第一版，依赖表名和字段名可读；如果新库命名完全无意义，
仍需要人工补充映射或由 LLM 进一步推断。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.data_gateway.gateway import _validate_table, describe_table, list_tables

_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MANUAL_MAPPING_PATH = Path.cwd() / 'data_mapping.json'

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

_CACHE: dict[str, dict[str, Any]] = {}


def load_manual_mapping() -> dict[str, Any]:
    """读取可选的人工映射配置 data_mapping.json。

    格式：
    {
      "plan": {"table": "products", "columns": {"id": "product_id", "name": "title"}},
      "shop": {"table": "stores", "columns": {"id": "store_id"}}
    }
    """
    if not _MANUAL_MAPPING_PATH.exists():
        return {}
    try:
        data = json.loads(_MANUAL_MAPPING_PATH.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _apply_manual_mapping(result: dict[str, Any]) -> dict[str, Any]:
    manual = load_manual_mapping()
    for entity_name, override in manual.items():
        if not isinstance(override, dict):
            continue
        ent = result.setdefault('entities', {}).setdefault(entity_name, {'table': None, 'columns': {}, 'confidence': 0})
        if isinstance(override.get('table'), str) and override['table']:
            ent['table'] = override['table']
            ent['manual_table'] = True
        if isinstance(override.get('columns'), dict):
            ent.setdefault('columns', {}).update(override['columns'])
            ent['manual_columns'] = True
        ent['confidence'] = 1.0
    return result


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


def _pick_table(tables: list[str], entity: dict[str, Any]) -> str | None:
    best = None
    best_score = 0
    for t in tables:
        s = _score_table(t, entity['table_hints'])
        if s > best_score:
            best = t
            best_score = s
    return best


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


def infer_mapping(force_refresh: bool = False) -> dict[str, Any]:
    """自动推断未知数据库到标准业务实体的映射。"""
    if not force_refresh and _CACHE:
        return _CACHE
    tables = list_tables()
    result: dict[str, Any] = {'tables': tables, 'entities': {}}
    for entity_name, entity_def in CANONICAL_ENTITIES.items():
        table = _pick_table(tables, entity_def)
        if not table:
            result['entities'][entity_name] = {'table': None, 'columns': {}, 'confidence': 0}
            continue
        try:
            desc = describe_table(table)
        except Exception:
            result['entities'][entity_name] = {'table': table, 'columns': {}, 'confidence': 0}
            continue
        columns = desc.get('columns', [])
        col_map: dict[str, str] = {}
        found = 0
        for canonical, candidates in entity_def['columns'].items():
            col = _pick_column(columns, candidates)
            if col:
                col_map[canonical] = col
                found += 1
        total = len(entity_def['columns'])
        confidence = round(found / total, 2) if total else 0
        result['entities'][entity_name] = {
            'table': table,
            'columns': col_map,
            'confidence': confidence,
        }
    _apply_manual_mapping(result)
    _CACHE.clear()
    _CACHE.update(result)
    return result


def _build_select(entity: str, mapping: dict[str, Any], limit: int, keyword: str | None = None, search_columns: list[str] | None = None) -> tuple[str, list[Any]]:
    ent = mapping['entities'].get(entity)
    if not ent or not ent.get('table'):
        raise ValueError(f'cannot auto-map entity: {entity}')
    table = ent['table']
    if not _IDENTIFIER.match(table):
        raise PermissionError('invalid table name')
    col_map = ent.get('columns') or {}
    if not col_map:
        raise ValueError(f'no columns mapped for entity: {entity}')
    aliases = []
    for canonical, actual in col_map.items():
        if not _IDENTIFIER.match(actual):
            raise PermissionError(f'invalid column name: {actual}')
        aliases.append(f'"{actual}" AS "{canonical}"')
    sql = f'SELECT {", ".join(aliases)} FROM "{table}"'
    params: list[Any] = []
    if keyword:
        cols = search_columns or [col_map.get('name') or col_map.get('id')]
        cols = [c for c in cols if c and _IDENTIFIER.match(c)]
        if cols:
            where = ' OR '.join(f'"{c}" LIKE ?' for c in cols)
            sql += f' WHERE {where}'
            params.extend([f'%{keyword}%'] * len(cols))
    sql += ' LIMIT ?'
    params.append(max(1, min(int(limit or 1), 100)))
    return sql, params


def auto_query(entity: str, keyword: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """按自动映射查询指定业务实体，返回标准字段的 dict 列表。"""
    mapping = infer_mapping()
    sql, params = _build_select(entity, mapping, limit, keyword)
    from backend.storage.db import get_conn
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def auto_search_plans(keyword: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """自动映射后搜索方案/商品，返回标准 plan 字段。"""
    mapping = infer_mapping()
    ent = mapping['entities'].get('plan')
    if not ent or not ent.get('table'):
        return []
    cols = [ent['columns'].get(c) for c in ('name', 'desc', 'tags')]
    sql, params = _build_select('plan', mapping, limit, keyword, search_columns=[c for c in cols if c])
    from backend.storage.db import get_conn
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def auto_search_shops(keyword: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """自动映射后搜索店铺，返回标准 shop 字段。"""
    mapping = infer_mapping()
    ent = mapping['entities'].get('shop')
    if not ent or not ent.get('table'):
        return []
    cols = [ent['columns'].get(c) for c in ('name', 'intro', 'address')]
    sql, params = _build_select('shop', mapping, limit, keyword, search_columns=[c for c in cols if c])
    from backend.storage.db import get_conn
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _build_insert(entity: str, mapping: dict[str, Any], data: dict[str, Any]) -> tuple[str, list[Any]]:
    ent = mapping['entities'].get(entity)
    if not ent or not ent.get('table'):
        raise ValueError(f'cannot auto-map entity for write: {entity}')
    table = ent['table']
    if not _IDENTIFIER.match(table):
        raise PermissionError('invalid table name')
    col_map = ent.get('columns') or {}
    if not col_map:
        raise ValueError(f'no columns mapped for entity: {entity}')
    cols = []
    params: list[Any] = []
    for canonical, value in data.items():
        actual = col_map.get(canonical)
        if not actual:
            continue
        if not _IDENTIFIER.match(actual):
            raise PermissionError(f'invalid column name: {actual}')
        cols.append(actual)
        params.append(value)
    if not cols:
        raise ValueError(f'no writable mapped columns for entity: {entity}')
    col_sql = ', '.join(f'"{c}"' for c in cols)
    placeholders = ', '.join('?' for _ in cols)
    sql = f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})'
    return sql, params


def auto_create_order(user_id: str, plan_id: str, total_price: float, status: str = 'created', order_id: str | None = None, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """按自动映射创建订单。

    安全设计：只有 data_mapping.json 中存在 ``"write_enabled": true`` 时才允许写入，
    避免智能体在未知数据库上误写。
    """
    manual = load_manual_mapping()
    if not manual.get('write_enabled'):
        raise PermissionError('order write is disabled; set "write_enabled": true in data_mapping.json to enable')
    mapping = infer_mapping()
    if 'order' not in mapping.get('entities', {}):
        raise ValueError('order entity is not mapped')
    oid = order_id or f"O{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6]}"
    data = {
        'id': oid,
        'user_id': user_id,
        'plan_id': plan_id,
        'total_price': float(total_price),
        'status': status,
        'created_at': datetime.now(UTC).isoformat(timespec='seconds'),
    }
    sql, params = _build_insert('order', mapping, data)
    from backend.storage.db import get_conn
    conn = get_conn()
    conn.execute(sql, params)
    conn.commit()

    inserted_items = 0
    if items and isinstance(manual.get('order_items'), dict):
        item_ent = manual['order_items']
        item_table = item_ent.get('table')
        item_cols = item_ent.get('columns') or {}
        if item_table and _IDENTIFIER.match(item_table) and item_cols:
            for it in items:
                row = {
                    'id': it.get('id') or f"OI{uuid.uuid4().hex[:10]}",
                    'order_id': oid,
                    'plan_id': it.get('plan_id') or plan_id,
                    'name': it.get('name', ''),
                    'price': it.get('price', 0),
                    'qty': it.get('qty', 1),
                }
                cols = []
                vals = []
                for canonical, value in row.items():
                    actual = item_cols.get(canonical)
                    if actual and _IDENTIFIER.match(actual):
                        cols.append(actual)
                        vals.append(value)
                if not cols:
                    continue
                col_sql = ', '.join(f'"{c}"' for c in cols)
                placeholders = ', '.join('?' for _ in cols)
                conn.execute(f'INSERT INTO "{item_table}" ({col_sql}) VALUES ({placeholders})', vals)
                inserted_items += 1
            conn.commit()

    return {'order_id': oid, 'inserted': True, 'inserted_items': inserted_items}


def auto_list_orders(user_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """按自动映射读取订单列表；若提供 user_id，尽量按用户过滤。"""
    mapping = infer_mapping()
    if 'order' not in mapping.get('entities', {}):
        return []
    ent = mapping['entities']['order']
    sql, params = _build_select('order', mapping, limit)
    if user_id and ent.get('columns', {}).get('user_id'):
        uid_col = ent['columns']['user_id']
        if _IDENTIFIER.match(uid_col):
            sql = sql.replace(' LIMIT ?', f' WHERE "{uid_col}" = ? LIMIT ?')
            params = [user_id] + params
    from backend.storage.db import get_conn
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
