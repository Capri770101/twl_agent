"""skills/skill_order.py —— 下单技能（独立自包含模块）。

作为「技能」而非普通工具注册：自描述（docstring + schema）+ 自注册（@register_tool）。
职责边界清晰：
- 组装订单数据（从仓库取方案/店铺，计算金额）
- 写入 orders 表
- 返回 pay_jump 参数（小程序的 /pages/order/confirm 跳转信息）
**不直接调用微信支付**——支付由小程序承接，后端只负责把订单和跳转参数交给前端。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from agent.tools import _resolve_session_plan, register_tool
from backend.config import settings
from backend.storage.db import get_conn
from backend.storage.repository import repo

logger = logging.getLogger('skills.order')

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')

def _plan_price(plan: dict) -> float:
    """方案金额：预设方案取 price；DIY 方案用 budget 兜底；兜底查 DB。"""
    pid = plan.get('plan_id', '')
    if isinstance(plan.get('price'), (int, float)) and plan['price'] > 0:
        return float(plan['price'])
    if isinstance(plan.get('budget'), (int, float)) and plan['budget'] > 0:
        return float(plan['budget'])
    breakdown = plan.get('budget_breakdown') or {}
    if isinstance(breakdown.get('total_estimate'), (int, float)):
        return float(breakdown['total_estimate'])
    m = re.search('(\\d+(?:\\.\\d+)?)', str(plan.get('estimated_price', '')))
    if m:
        return float(m.group(1))
    if pid:
        try:
            row = get_conn().execute('SELECT price FROM plans WHERE id=?', (pid,)).fetchone()
            if row and isinstance(row['price'], (int, float)) and (row['price'] > 0):
                return float(row['price'])
        except Exception:
            pass
    return 0.0

def _extract_flower_names(plan: dict) -> list[dict]:
    """从 DIY 方案中提取花材列表（带 qty），兼容 in-memory 和 DB 两种格式。

    返回 [{name, qty}] — qty 为该花材支数，缺失时默认 1。
    """
    design = plan.get('design') or {}
    flowers_raw = plan.get('flowers') or []
    seen: dict[str, int] = {}
    for key in ('main_flowers', 'fillers', 'foliage'):
        for f in design.get(key, []):
            if not isinstance(f, dict):
                continue
            name = f.get('name', '')
            if not name:
                continue
            qty = int(f.get('qty', 1)) if isinstance(f.get('qty'), (int, float)) else 1
            seen[name] = seen.get(name, 0) + qty
    for f in flowers_raw:
        if isinstance(f, dict) and f.get('name'):
            name = f['name']
            qty = int(f.get('qty', 1)) if isinstance(f.get('qty'), (int, float)) else 1
            seen[name] = seen.get(name, 0) + qty
    return [{'name': n, 'qty': q} for n, q in seen.items()]

def _match_flowers_to_shop(flower_list: list[dict], shop_id: str) -> dict:
    """将花材列表与店铺在售商品做匹配，返回匹配结果。

    flower_list: [{name, qty}] —— _extract_flower_names 的输出。
    匹配策略（优先级从高到低）：
    1. 商品名包含花材名
    2. 标签包含花材名
    3. 子串兜底：花材名的任一词根（≥2字）出现在商品名中

    返回:
      matched: [{plan_id, name, price, image, flower, qty, line_total}]
      missing: [{name, qty}]
      estimated_cost: 所有已匹配商品 line_total 之和
      coverage: 已匹配花材种类数 / 总花材种类数
      budget_total: 方案 budget_breakdown.total_estimate（若有）
    """
    conn = get_conn()
    items = conn.execute("SELECT p.id, p.name, p.price, p.tags, p.desc, p.effect_image_url\n           FROM plans p JOIN shop_plans sp ON p.id = sp.plan_id\n           WHERE sp.shop_id=? AND sp.status='on'", (shop_id,)).fetchall()
    matched = []
    missing = []
    total_cost = 0.0
    for fl_info in flower_list:
        fl = fl_info['name']
        qty = fl_info.get('qty', 1)
        best = None
        for item in items:
            name = item['name'] or ''
            tags_raw = item['tags'] or ''
            if tags_raw.startswith('['):
                try:
                    tags_list = json.loads(tags_raw)
                except json.JSONDecodeError:
                    tags_list = [t.strip().strip('"') for t in tags_raw.strip('[]').split(',')]
            else:
                tags_list = [t.strip() for t in tags_raw.split(',') if t.strip()]
            if fl in name or fl in tags_raw or fl in tags_list:
                best = {'plan_id': item['id'], 'name': name, 'price': item['price'], 'image': item['effect_image_url'] or '', 'flower': fl, 'qty': qty, 'line_total': round(item['price'] * qty, 2)}
                break
        if not best:
            for item in items:
                name = item['name'] or ''
                candidates = {fl[i:i + 2] for i in range(len(fl) - 1)} | {fl[i:i + 3] for i in range(max(0, len(fl) - 2))}
                if any(c in name for c in candidates if len(c) >= 2):
                    best = {'plan_id': item['id'], 'name': name, 'price': item['price'], 'image': item['effect_image_url'] or '', 'flower': fl, 'qty': qty, 'line_total': round(item['price'] * qty, 2)}
                    break
        if best:
            matched.append(best)
            total_cost += best['line_total']
        else:
            missing.append({'name': fl, 'qty': qty})
    return {'matched': matched, 'missing': missing, 'estimated_cost': round(total_cost, 2), 'coverage': round(len(matched) / len(flower_list), 2) if flower_list else 0}

def _build_detailed_items(plan: dict) -> list[dict]:
    """非 DIY 方案：返回单条方案项。"""
    pid = plan.get('plan_id', '')
    return [{'plan_id': pid, 'name': plan.get('name', '花束'), 'role': '方案', 'price': _plan_price(plan), 'unit_price': _plan_price(plan), 'qty': 1, 'image': plan.get('effect_image_url') or plan.get('image') or ''}]

def _build_diy_order_items(plan: dict, shop_id: str) -> tuple[list[dict], float, float, list[dict]]:
    """DIY 方案：从 shop 单品匹配组装订单明细。

    返回 (items, total, coverage, missing)：
    - items: 每条 = {plan_id, name, role, price, unit_price, qty, product_id, image}
    - total: 全覆盖时用店铺实际价之和；部分覆盖时也用已匹配价之和（调用方负责拦截）
    - coverage: 花材覆盖率（0~1）
    - missing: 缺失花材列表 [{name, qty}]

    当花材覆盖率 < 100% 时，调用方（create_order）应拦截并提示用户，
    而非用预算估算价创建订单。
    """
    flower_infos = _extract_flower_names(plan)
    if not flower_infos:
        return (_build_detailed_items(plan), _plan_price(plan), 1.0, [])
    match_result = _match_flowers_to_shop(flower_infos, shop_id)
    matched = match_result['matched']
    missing = match_result['missing']
    items: list[dict] = []
    pid = plan.get('plan_id', '')
    for m in matched:
        items.append({'plan_id': pid, 'name': m['name'], 'role': m['flower'], 'price': m['line_total'], 'unit_price': m['price'], 'qty': m['qty'], 'product_id': m['plan_id'], 'image': m.get('image', '')})
    for fl in missing:
        items.append({'plan_id': pid, 'name': f"{fl['name']}（店铺暂无）", 'role': fl['name'], 'price': 0, 'unit_price': 0, 'qty': fl['qty']})
    coverage = match_result.get('coverage', 0)
    matched_cost = match_result['estimated_cost']
    if coverage >= 1.0:
        total = matched_cost
    else:
        total = matched_cost
    if not items:
        items = _build_detailed_items(plan)
        total = _plan_price(plan)
    elif total <= 0:
        total = matched_cost or _plan_price(plan)
    return (items, total, coverage, match_result.get('missing', []))

@register_tool(name='create_order', description='组装订单并生成小程序支付跳转参数（pay_jump）。不直接调用微信支付，支付由小程序承接。', parameters={'type': 'object', 'properties': {'shop_id': {'type': 'string', 'description': "店铺 ID，或 'first' 表示推荐列表第一家"}, 'plan_id': {'type': 'string', 'description': "方案 ID，或 'latest' 表示当前方案"}, 'plan_type': {'type': 'string', 'description': 'existing | diy'}}, 'required': ['shop_id', 'plan_id', 'plan_type']}, inject_context=True, tags=['order'])
async def create_order(shop_id: str, plan_id: str, plan_type: str, _context: dict | None=None) -> str:
    """组装订单并写入 orders 表，返回 order_card + pay_jump 数据。

    DIY 方案：从 shop 单品匹配花材，用店铺实际价格计算总价。
    现有方案：直接使用方案价格。
    """
    user_id = (_context or {}).get('user_id', 'anonymous')
    shop = await repo.get_shop(shop_id) if shop_id != 'first' else (await repo.list_shops(None, None))[0]
    plan = await _resolve_session_plan(plan_id, _context)
    if not shop or not plan:
        return json.dumps({"error": "店铺或方案不存在"}, ensure_ascii=False)

    if plan.get("diy"):
        plan_type = "diy"

    # DIY 方案：从 shop 单品匹配组装；现有方案：直接用方案价格
    coverage = 1.0
    missing_flowers: list[dict] = []
    if plan_type == 'diy':
        items, total, coverage, missing_flowers = _build_diy_order_items(plan, shop['shop_id'])
        if coverage < 1.0:
            return json.dumps({'error': 'insufficient_coverage', 'message': '该店铺缺少部分花材，无法完成此 DIY 方案。', 'coverage': round(coverage, 2), 'missing_flowers': missing_flowers, 'suggestion': '您可以选择：1）从不同店铺分别购买缺失的花材，自行 DIY 制作；2）更换其他花材方案或调整设计。'}, ensure_ascii=False)
        try:
            from backend.storage.diy import save_diy_plan
            _res = await save_diy_plan(plan, user_id)
            if _res.get('plan_id'):
                plan['plan_id'] = _res['plan_id']
        except Exception:
            logger.warning('[skill_order] DIY 方案落库失败（不影响下单）', exc_info=True)
    else:
        total = _plan_price(plan)
        items = _build_detailed_items(plan)

    # 统一走 commerce.create_order()：自动设置 expires_at、物流事件、通知、优惠券、购物车清理
    from backend.storage import commerce
    from backend.storage.repository import repo as _repo

    plan_obj = await _repo.get_plan(plan["plan_id"])
    if plan_type == "diy":
        # DIY 方案：订单项以 DIY 方案本身为主（plan_id 指向 DIY 方案），
        # 附带各花材明细供详情页展示。commerce 会对 DIY plan_id 校验；
        # 若 DIY 方案未落库则用第一个已有店铺单品兜底。
        diy_pid = plan.get("plan_id", "")
        # 确保 DIY 方案已落库
        if not plan_obj:
            try:
                from backend.storage.diy import save_diy_plan
                _res = await save_diy_plan(plan, user_id)
                if _res.get("plan_id"):
                    plan["plan_id"] = _res["plan_id"]
                    diy_pid = _res["plan_id"]
            except Exception:  # noqa: BLE001
                logger.warning("[skill_order] DIY 方案落库失败（不影响下单）", exc_info=True)
            plan_obj = await _repo.get_plan(diy_pid)
        # 如果 DIY 方案在 plans 表找不到（save_diy_plan 可能没写 plans），
        # 用第一个已有店铺单品代替 plan_id，花材明细附在 items 里。
        if not plan_obj and items:
            primary_pid = items[0].get("product_id") or items[0].get("plan_id")
            commerce_items = [{"plan_id": primary_pid, "qty": 1, "item_id": None}]
        else:
            commerce_items = [{"plan_id": diy_pid, "qty": 1, "item_id": None}]
        try:
            from backend.storage.diy import mark_diy_plan_ordered, save_as_template
            await mark_diy_plan_ordered(plan["plan_id"])
            await save_as_template(plan["plan_id"])
        except Exception:  # noqa: BLE001
            logger.warning("[skill_order] 标记 DIY 方案成交/沉淀模板失败", exc_info=True)
    else:
        # 现有方案：直接用方案 plan_id
        commerce_items = [{"plan_id": plan["plan_id"], "qty": 1, "item_id": None}]

    try:
        order = await commerce.create_order(
            user_id=user_id,
            items=commerce_items,
            shop_id=shop['shop_id'],
        )
    except Exception as exc:
        return json.dumps({"error": f"下单失败: {exc}"}, ensure_ascii=False)
    order_id = order["order_id"]
    total = order["total_price"]
    discount = order.get("discount", 0)

    pay_jump = {
        "order_id": order_id,
        "page_path": settings.pay_page_path,
        "params": {
            "order_id": order_id,
            "total_price": total,
            "discount": discount,
            "shop_id": shop["shop_id"],
        },
    }
    logger.info("[skill_order] 订单 %s 已创建 user=%s shop=%s total=%.2f discount=%.2f", order_id, user_id, shop["shop_id"], total, discount)

    return json.dumps(
        {
            "order_id": order_id,
            "plan_name": plan.get("name", ""),
            "items": items,
            "total_price": total,
            "discount": discount,
            "plan_type": plan_type,
            "pay_jump": pay_jump,
            "effect_image_url": plan.get("effect_image_url") or plan.get("result_url") or "",
            "coverage": coverage,
            "missing_flowers": missing_flowers,
        },
        ensure_ascii=False,
    )
