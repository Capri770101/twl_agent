"""独立封装版 diy.py。"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from backend.storage.db import transaction

logger = logging.getLogger('storage.diy')


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def _flower_rows(design: dict) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bucket, key in (('主花', 'main_flowers'), ('填充', 'fillers'), ('叶材', 'foliage')):
        for f in design.get(key) or []:
            if isinstance(f, dict):
                rows.append({'bucket': bucket, 'name': f.get('name'), 'ratio': f.get('ratio')})
            else:
                rows.append({'bucket': bucket, 'name': f, 'ratio': None})
    return rows


def _row_to_plan(row: Any) -> dict[str, Any]:
    flowers = json.loads(row['flowers']) if row['flowers'] else []
    design = {
        'main_flowers': [{'name': f['name'], 'ratio': f.get('ratio')} for f in flowers if f.get('bucket') == '主花'],
        'fillers': [{'name': f['name'], 'ratio': f.get('ratio')} for f in flowers if f.get('bucket') == '填充'],
        'foliage': [{'name': f['name'], 'ratio': f.get('ratio')} for f in flowers if f.get('bucket') == '叶材'],
        'color_scheme': json.loads(row['color_scheme']) if row['color_scheme'] else [],
        'packaging': row['packaging'],
        'meaning': row['meaning'],
        'difficulty': row['difficulty'],
        'est_time': row['est_time'],
        'shelf_life': row['shelf_life'],
        'suitable_for': json.loads(row['suitable_for']) if row['suitable_for'] else [],
        'caution': row['caution'],
        'mood_tags': json.loads(row['mood_tags']) if row['mood_tags'] else [],
    }
    budget = row['budget']
    effect_prompt = f"{row['style'] or '定制'}风格花束，主花为{', '.join(f['name'] for f in flowers if f.get('bucket') == '主花') or '玫瑰'}，搭配{', '.join(f['name'] for f in flowers if f.get('bucket') == '填充') or '满天星'}与{', '.join(f['name'] for f in flowers if f.get('bucket') == '叶材') or '尤加利'}，色调{'/'.join(design['color_scheme']) or '温柔粉'}，{row['packaging'] or '花束'}包装，背景干净柔和，摄影级静物，高级感"
    return {
        'plan_id': row['id'],
        'name': row['name'],
        'diy': True,
        'recipient': row['recipient'],
        'occasion': row['occasion'],
        'style': row['style'],
        'budget_num': budget,
        'design': design,
        'estimated_price': f'约 {int(budget)} 元' if budget else '',
        'desc': f"{row['name']}：{design['meaning'] or '我的 DIY 方案'}。",
        'diy_steps': json.loads(row['diy_steps']) if row['diy_steps'] else [],
        'care_tips': row['care_tips'],
        'card_message': row['card_message'],
        'card_image_url': row['card_image_url'],
        'budget_breakdown': json.loads(row['budget_breakdown']) if row['budget_breakdown'] else {},
        'effect_image_url': row['effect_image_url'],
        'effect_prompt': effect_prompt,
        'price': budget,
        'tags': [row['style'], row['occasion'], row['recipient']],
        'requirement': row['requirement'],
        'order_count': row['order_count'],
        'status': row['status'],
    }


async def save_diy_plan(plan: dict, user_id: str) -> dict[str, Any]:
    plan_id = str(plan.get('plan_id') or '') if plan else ''
    if not plan or not user_id:
        return {'saved': False, 'duplicate': False, 'plan_id': plan_id}
    if not plan_id:
        plan_id = 'DIY_' + uuid.uuid4().hex[:6]
    d = plan.get('design') or plan
    fp = plan.get('fingerprint') or uuid.uuid4().hex
    now = _now()
    with transaction() as c:
        row = c.execute('SELECT id, effect_image_url FROM diy_plans WHERE user_id=? AND fingerprint=?', (user_id, fp)).fetchone()
        if row:
            return {'saved': False, 'duplicate': True, 'plan_id': row['id']}
        c.execute('INSERT INTO diy_plans(id, user_id, fingerprint, name, requirement, recipient, occasion, style, budget, color_scheme, flowers, packaging, meaning, diy_steps, care_tips, card_message, card_image_url, budget_breakdown, effect_image_url, difficulty, est_time, shelf_life, suitable_for, caution, mood_tags, status, order_count, created_at, confirmed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (plan_id, user_id, fp, str(plan.get('name') or '未命名方案'), str(plan.get('requirement') or ''), str(plan.get('recipient') or ''), str(plan.get('occasion') or ''), str(plan.get('style') or ''), plan.get('budget_num'), json.dumps(d.get('color_scheme') or [], ensure_ascii=False), json.dumps(_flower_rows(d), ensure_ascii=False), str(d.get('packaging') or ''), str(d.get('meaning') or ''), json.dumps(plan.get('diy_steps') or [], ensure_ascii=False), str(plan.get('care_tips') or ''), str(plan.get('card_message') or ''), str(plan.get('card_image_url') or ''), json.dumps(plan.get('budget_breakdown') or {}, ensure_ascii=False), plan.get('effect_image_url'), str(d.get('difficulty') or ''), d.get('est_time'), str(d.get('shelf_life') or ''), json.dumps(d.get('suitable_for') or [], ensure_ascii=False), str(d.get('caution') or ''), json.dumps(d.get('mood_tags') or [], ensure_ascii=False), 'confirmed', 0, now, now))
    return {'saved': True, 'duplicate': False, 'plan_id': plan_id}


async def mark_diy_plan_ordered(plan_id: str) -> None:
    if not plan_id:
        return


async def save_as_template(plan_id: str) -> None:
    return None


async def get_diy_plan(plan_id: str) -> dict[str, Any] | None:
    with transaction() as c:
        row = c.execute('SELECT * FROM diy_plans WHERE id=?', (plan_id,)).fetchone()
    return _row_to_plan(row) if row else None


async def search_diy_plans(user_id: str, requirement: Any | None = None, limit: int = 3) -> list[dict[str, Any]]:
    if not user_id:
        return []
    with transaction() as c:
        rows = c.execute('SELECT * FROM diy_plans WHERE user_id=? ORDER BY order_count DESC, confirmed_at DESC', (user_id,)).fetchall()
    return [_row_to_plan(r) for r in rows][:limit]


def list_proven_plans(limit: int = 20) -> list[dict[str, Any]]:
    with transaction() as c:
        rows = c.execute('SELECT * FROM diy_plans ORDER BY order_count DESC, confirmed_at DESC LIMIT ?', (limit,)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        p = _row_to_plan(r)
        out.append({'id': p['plan_id'], 'name': p['name'], 'style': p['style'], 'recipient': p['recipient'], 'occasion': p['occasion'], 'budget': p['budget_num'], 'flowers': [f['name'] for f in p['design']['main_flowers']], 'color_scheme': p['design']['color_scheme'], 'packaging': p['design']['packaging'], 'meaning': p['design']['meaning'], 'status': r['status'], 'order_count': r['order_count'], 'confirmed_at': r['confirmed_at']})
    return out
