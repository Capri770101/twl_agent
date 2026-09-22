"""从店铺在售商品摘要提取可控的视觉风格画像。"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()
_TTL = 900


def _sources() -> list[str]:
    from agent.agent import _platform_source_ids
    return _platform_source_ids()


def _tokens(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for row in rows:
        for key in keys:
            value = row.get(key)
            if isinstance(value, list):
                values.extend(str(x) for x in value)
            elif value:
                values.append(str(value))
    return values


def build_style_profile(rows: list[dict[str, Any]], shop_id: str) -> dict[str, Any]:
    text = ' '.join(_tokens(rows, ('name', 'subtitle', 'description', 'tags', 'flowers', 'packaging')))
    colors = [x for x in ('白', '绿', '粉', '红', '香槟', '紫', '黄', '橙', '蓝') if x in text]
    styles = [x for x in ('韩式', '自然', '北欧', '法式', '复古', '现代', '简约', '奶油', '轻奢') if x in text]
    packaging = [x for x in ('雾面纸', '牛皮纸', '礼盒', '花篮', '手提袋', '丝带') if x in text]
    names = [str(row.get('name') or '') for row in rows if row.get('name')][:8]
    return {
        'shop_id': shop_id,
        'sample_count': len(rows),
        'styles': list(dict.fromkeys(styles)),
        'colors': list(dict.fromkeys(colors)),
        'packaging': list(dict.fromkeys(packaging)),
        'product_names': names,
        'instruction': '参考该店近期在售商品的视觉语言：' +
                       '、'.join(dict.fromkeys(styles + colors + packaging)) if (styles or colors or packaging) else '',
    }


def get_shop_style_profile(shop_id: str, *, force: bool = False) -> dict[str, Any]:
    sid = str(shop_id or '').strip()
    if not sid:
        return {}
    sources = _sources()
    key = (','.join(sources), sid)
    hit = _CACHE.get(key)
    if hit and not force and time.time() - hit[0] < _TTL:
        return hit[1]
    from backend.data_gateway.external import query_external_entity
    rows: list[dict[str, Any]] = []
    for source in sources:
        try:
            rows = query_external_entity(source, 'plan', limit=30, shop_id=sid)
            if rows:
                break
        except Exception:
            continue
    profile = build_style_profile(rows, sid)
    with _LOCK:
        _CACHE[key] = (time.time(), profile)
    return profile


def style_prompt_suffix(profile: dict[str, Any]) -> str:
    if not profile or not profile.get('instruction'):
        return ''
    return (
        f"\n【当前店铺风格参考】{profile['instruction']}。"
        '这只是风格参考，不是商品复制；必须严格保持本方案的花材、支数、配色和包装约束，'
        '只借鉴近期商品的构图、留白、包装质感和整体摄影风格，不得把参考商品的花材替换进本方案。'
    )
