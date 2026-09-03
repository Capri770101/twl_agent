"""独立封装版 storage/config.py。"""
from __future__ import annotations

import json
from typing import Any

from backend.storage.db import transaction


K_DELIVERY = 'delivery_options'
K_SHIPPING = 'shipping_fee'
K_COUPON = 'coupon_rules'
K_FAQS = 'faqs'
K_ANNOUNCE = 'announcements'
K_REC_WEIGHTS = 'recommend_weights'
K_SERVICE = 'service'

DEFAULTS: dict[str, Any] = {
    K_DELIVERY: ['今天 18:00–20:00', '明天 10:00–12:00'],
    K_SHIPPING: 5,
    K_COUPON: {},
    K_FAQS: [],
    K_ANNOUNCE: [],
    K_REC_WEIGHTS: {'w_distance': 0.4, 'w_pref': 0.4, 'w_heat': 0.2},
    K_SERVICE: {'hotline': '400-800-1234', 'hours': '每日 9:00 - 21:00'},
}


async def _load(key: str) -> Any | None:
    with transaction() as c:
        row = c.execute('SELECT value FROM operations_config WHERE key=?', (key,)).fetchone()
    if not row or not row['value']:
        return None
    try:
        return json.loads(row['value'])
    except (json.JSONDecodeError, TypeError):
        return None


async def _save(key: str, value: Any) -> None:
    with transaction() as c:
        c.execute(
            'INSERT INTO operations_config(key, value) VALUES (?,?) '
            'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
            (key, json.dumps(value, ensure_ascii=False))
        )


async def get_config(key: str, default: Any = None) -> Any:
    v = await _load(key)
    return v if v is not None else default


async def set_config(key: str, value: Any) -> Any:
    await _save(key, value)
    return value


async def public_config() -> dict[str, Any]:
    return {
        'delivery_options': await get_config(K_DELIVERY, DEFAULTS[K_DELIVERY]),
        'shipping_fee': await get_config(K_SHIPPING, DEFAULTS[K_SHIPPING]),
        'coupon_rules': await get_config(K_COUPON, DEFAULTS[K_COUPON]),
        'faqs': await get_config(K_FAQS, DEFAULTS[K_FAQS]),
        'announcements': await get_config(K_ANNOUNCE, DEFAULTS[K_ANNOUNCE]),
        'recommend_weights': await get_config(K_REC_WEIGHTS, DEFAULTS[K_REC_WEIGHTS]),
        'service': await get_config(K_SERVICE, DEFAULTS[K_SERVICE]),
    }


async def admin_config() -> dict[str, Any]:
    return await public_config()
