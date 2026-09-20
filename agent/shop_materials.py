"""从平台商品的用料文案，推断店铺「可提供的原料花材」。

为什么（2026-09-17，Capri 需求）：锁店场景下 DIY 方案的原料必须是该店能买到的，
否则方案落不了地 —— 客户拿着方案去这家店配不齐，方案就只是张好看的图。
平台的商品数据里**没有结构化花材清单**，只有一句人读的用料描述
（如 ``"11枝粉玫瑰花混搭"``）；店铺字段里虽然存了花材 ID（``xf011`` 这类），
但对应的名字要走鉴权接口，匿名拿不到。因此改为：从**该店在售商品的文案**
（名称 / 副标题 / 用料描述）里提取花材名，作为「该店可提供原料」的依据。

局限（务必知情，别当成权威数据）：
- 这是**文本推断**，不是平台的权威花材清单：文案里没写到的花材会漏判。
- 平台目前只上架**成品花束**，没有散装花材商品，所以「可买到」严格说是
  「这家店的花束里用过这种花材」——它已经是匿名数据里最接近的近似。
- 只覆盖**花材**；包装纸 / 缎带等物料平台没有数据，不参与判定。
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# 缓存时长（秒）：店铺在售花材变化很慢，没必要每轮对话都拉一遍 100 条商品。
_TTL = float(os.environ.get('SHOP_MATERIALS_TTL', '900'))
# 单次拉取的商品条数（平台上限 100）。
_LIMIT = int(os.environ.get('SHOP_MATERIALS_LIMIT', '100'))

_CACHE: dict[str, tuple[float, set[str]]] = {}
_LOCK = threading.Lock()
_TERM_MAP: dict[str, str] | None = None


def _term_map() -> dict[str, str]:
    """别名 → 规范花材名（取自知识库，含 aliases）。

    规范名统一到知识库的 ``name``，避免「粉玫瑰」和「玫瑰」在方案里被当成两种花。
    单字别名（如「菊」）丢弃，太容易误命中。
    """
    global _TERM_MAP
    if _TERM_MAP is None:
        from agent.knowledge import query_knowledge

        mapping: dict[str, str] = {}
        for f in query_knowledge('flower', '').get('results', []):
            name = str(f.get('name') or '').strip()
            if not name:
                continue
            mapping[name] = name
            for alias in f.get('aliases') or []:
                alias = str(alias).strip()
                if len(alias) >= 2:
                    mapping.setdefault(alias, name)
        _TERM_MAP = mapping
    return _TERM_MAP


def materials_in_text(text: str) -> set[str]:
    """从一段文案里提取花材（返回知识库规范名）。

    Args:
        text: 任意商品文案（名称 / 副标题 / 用料描述拼接）。

    Returns:
        命中的花材规范名集合；无命中返回空集。
    """
    if not text:
        return set()
    return {canonical for alias, canonical in _term_map().items() if alias in text}


def _source_ids() -> list[str]:
    """已配置的平台数据源（延迟导入，规避与 agent.agent 的循环依赖）。"""
    try:
        from agent.agent import _platform_source_ids

        return _platform_source_ids()
    except Exception:  # noqa: BLE001 - 取不到就当作未配置，不影响主流程
        return []


def _fetch(shop_id: str) -> set[str] | None:
    """真正去平台拉该店在售商品并提取花材；失败返回 None（未知）。"""
    from backend.data_gateway.external import query_external_entity

    sources = _source_ids()
    if not sources:
        return None
    found: set[str] = set()
    ok = False
    for sid in sources:
        try:
            rows = query_external_entity(sid, 'plan', limit=_LIMIT, shop_id=shop_id)
        except Exception:  # noqa: BLE001 - 单个数据源失败不影响其它源
            logger.warning('[shop_materials] 拉取店铺 %s 在售商品失败（source=%s）', shop_id, sid, exc_info=True)
            continue
        ok = True
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            flowers = r.get('flowers')
            if isinstance(flowers, list):
                flowers = ' '.join(str(x) for x in flowers)
            text = ' '.join([
                str(r.get('name') or ''),
                str(r.get('subtitle') or ''),
                str(flowers or ''),
                str(r.get('description') or ''),
            ])
            found |= materials_in_text(text)
    return found if ok else None


def available_materials(shop_id: str) -> set[str] | None:
    """该店可提供的原料花材（知识库规范名）。

    Args:
        shop_id: 店铺 ID；空值直接返回 None。

    Returns:
        花材规范名集合；**查询失败 / 未配置数据源时返回 None**，表示「未知」而
        不是「没有」——调用方必须区分这两者，未知时不要做任何缺料标注。
    """
    sid = str(shop_id or '').strip()
    if not sid:
        return None
    now = time.time()
    from backend.data_gateway.access import cache_scope
    scope = cache_scope()
    key = sid if scope is None else (scope, sid)
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    with _LOCK:
        hit = _CACHE.get(key)  # 双检：并发下只让一个线程真正去拉
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
        found = _fetch(sid)
        if found is None:
            return None
        _CACHE[key] = (time.time(), found)
        logger.info('[shop_materials] 店铺 %s 可提供花材 %d 种：%s', sid, len(found), '、'.join(sorted(found)) or '(空)')
        return found


def clear_cache(shop_id: str | None = None) -> None:
    """清缓存（测试与数据更新后使用）。"""
    with _LOCK:
        if shop_id is None:
            _CACHE.clear()
        else:
            sid = str(shop_id).strip()
            for key in list(_CACHE):
                if key == sid or (isinstance(key, tuple) and key[1] == sid):
                    _CACHE.pop(key, None)
