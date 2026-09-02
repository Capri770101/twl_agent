"""店铺匹配工具。"""

from __future__ import annotations

import json

from agent.toolkit import register_tool
from agent.skills.skill_order import _match_flowers_to_shop
from backend.storage import memory
from backend.storage.repository import repo
from backend.storage.db import get_conn


@register_tool(name='search_shops', description='按距离、价格、服务评价综合排序推荐店铺；会结合用户位置与预算（来自结构化需求）做排序与过滤。', parameters={'type': 'object', 'properties': {'plan': {'type': 'string', 'description': '方案 ID；或 latest/latest_diy 表示使用最近方案'}}, 'required': ['plan']}, inject_context=True, tags=['shop'])
async def search_shops(plan: str='latest', _context: dict | None=None) -> str:
    req = (_context or {}).get('requirement')
    location = None
    if _context:
        location = _context.get('location') or (req.location if req else None)
    plan_obj = await repo.get_plan(plan) if plan not in ('latest', 'latest_diy', '', None) else None
    sid = (_context or {}).get('session_id', '')
    uid = (_context or {}).get('user_id', '')
    if plan_obj and sid:
        await memory.set_session_json(uid, sid, 'selected_plan', plan_obj)
    locked_shop = (_context or {}).get('shop_id')
    if locked_shop:
        shops = [{'shop_id': locked_shop, 'name': ''}]
    else:
        shops = await repo.list_shops(plan_obj, location, requirement=req)
    return json.dumps(shops[:3], ensure_ascii=False)


@register_tool(name='match_shop_items', description='根据 DIY 方案的花材需求，匹配各店铺库存中的单品（单支花束/配材/绿植），返回每家店的匹配结果：覆盖了哪些花材、缺哪些、总费用估算。用于 DIY 方案落地时推荐能实际提供所需花材的店铺。', parameters={'type': 'object', 'properties': {'flowers': {'type': 'string', 'description': 'DIY 方案的花材列表（JSON 数组或逗号分隔），如 "[\"红玫瑰\",\"满天星\",\"尤加利\"]" 或 "红玫瑰,满天星,尤加利"'}, 'shop_id': {'type': 'string', 'description': '指定店铺 ID（可选）；不指定则搜索所有店铺'}}, 'required': ['flowers']}, inject_context=True, tags=['shop', 'diy'])
def match_shop_items(flowers: str, shop_id: str | None=None, _context: dict | None=None) -> str:
    if flowers.startswith('['):
        try:
            raw_list = json.loads(flowers)
        except json.JSONDecodeError:
            raw_list = [f.strip() for f in flowers.strip('[]').split(',') if f.strip()]
    else:
        raw_list = [f.strip() for f in flowers.split(',') if f.strip()]
    flower_list = []
    for item in raw_list:
        if isinstance(item, dict):
            flower_list.append({'name': item.get('name', ''), 'qty': int(item.get('qty', 1)) if item.get('qty') else 1})
        elif isinstance(item, str) and item:
            flower_list.append({'name': item, 'qty': 1})
    if not flower_list:
        return {'error': '花材列表为空'}
    conn = get_conn()
    effective_shop = shop_id or ((_context or {}).get('shop_id') if _context else None)
    if effective_shop:
        shops = conn.execute('SELECT * FROM shops WHERE id=?', (effective_shop,)).fetchall()
    else:
        shops = conn.execute('SELECT * FROM shops').fetchall()
    results = []
    for s in shops:
        match_result = _match_flowers_to_shop(flower_list, s['id'])
        results.append({'shop_id': s['id'], 'shop_name': s['name'], 'matched': match_result['matched'], 'missing': match_result['missing'], 'coverage': match_result['coverage'], 'estimated_cost': match_result['estimated_cost']})
    results.sort(key=lambda x: (-x['coverage'], x['estimated_cost']))
    return results[:5]
