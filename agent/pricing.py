"""花材价格表：从平台在售商品反推「基础费 + 单支边际单价」。

为什么要有这个模块（Capri 2026-09-18 拍板）
------------------------------------------
原先 DIY 定价用三条硬编码常数（低 12 / 中 28 / 高 60 元/支）。
2026-09-18 用平台 1855 个在售商品实测，发现**高估 1.5~6.4 倍**（玫瑰实测 4.3 元/支，
我们写 28），导致两个后果：

1. DIY 报价严重脱离平台价格带（平台商品中位 108 元，我们动辄 300+）；
2. 「按预算反推支数」用虚高单价 → **花量算得远少于同价位现成品**
   （300 元按实测模型能买 50+ 支玫瑰，我们只算 6~10 支）。

→ 决策：**删掉代码里的定价，改成从店铺现有商品反推**。有确定店铺时分析该店商品、
算出该店的单支花材价格，该店后续 DIY 都用这套价；没锁店时用全网实测基线。

数据基础
--------
商品 ``flowers`` 字段是**带支数的配方文本**（不是人读描述）：
``"11枝香槟玫瑰4朵向日葵6枝碎冰蓝混搭花束"`` ——
1855 条中 1803 条非空，**1424 条（77%）可解析出「花材 + 支数」**。

定价模型：**两段式**
--------------------
实测玫瑰纯花束（n=499）：``总价 ≈ 35 + 4.3×支数``（r=0.96）；
康乃馨（n=90）：``总价 ≈ 46 + 2.6×支数``（r=0.92）。
即平台真实结构是 **基础费（包装/手工/配送）+ 边际单价×支数**，
而不是「单价 × 支数」—— 这正是「11 支 88 元」与「33 支 138 元」每支价不同的原因。

拟合方式：岭回归解 ``price ≈ base + Σ(支数_i × 单价_i)``，
用**全部可解析商品**（不只纯花材款），这样每种出现过的主花都能解出系数。

⚠️ 反推出来的是**含服务的零售价**（含包装/人工/配送），**不是花材成本价**。
   对外只能表述为「与平台在售价对齐」，不能说「这是成本」。
⚠️ 混搭商品是欠定方程，单条解不出，靠**多条商品联立**才有解 ——
   所以样本量直接决定可信度（见 ``PriceTable.samples``）。

分级兜底（依次降级，每级都写在 ``PriceTable.source`` 上）
--------------------------------------------------------
``override``  商家自定义价表（**为将来的商家工作台预留**，目前无来源）
``merchant``  该店在售商品实测（样本 ≥ ``_MIN_SAMPLES``）
``baseline``  全网实测基线（内置表，见 ``_BASELINE_UNIT``）
（基线表里没有的花材 → 按价格档兜底，见 ``_UNIT_BY_TIER``）

用法::

    table = price_table(shop_id)          # 拿价表
    table.base_fee                        # 基础费（元）
    table.unit_price('玫瑰')               # 单支价（元）
    table.price_of([('玫瑰', 11)])        # 便捷试算：35 + 11×4.3
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────────
# 价表缓存时长：店铺在售商品与价格变化很慢，没必要每轮对话都重算。
_TTL = float(os.environ.get('SHOP_PRICING_TTL', '1800'))
# 单次拉取的商品条数（平台硬上限 100，见 http_source.MAX_LIMIT）。
_LIMIT = int(os.environ.get('SHOP_PRICING_LIMIT', '100'))
# 该店拟合所需的最少可解析样本数；不足则降级到基线。
# 样本太少时岭回归会解出噪声系数（如把一次促销当成价格规律）。
_MIN_SAMPLES = int(os.environ.get('SHOP_PRICING_MIN_SAMPLES', '20'))
# 岭正则强度（只作用于花材系数，不作用于截距）：稳定小样本、抑制共线。
_RIDGE = 0.5
# 单价与基础费的合理上限（防极端拟合值污染报价）。
_MAX_UNIT = 80.0
_MAX_BASE = 200.0
_MIN_UNIT = 0.5
# 商品价格中位数的合理下限：低于它基本可判定是价格单位错误（见 fit_from_rows 自检）。
_MIN_SANE_PRICE = 5.0

# 「11枝粉玫瑰」→ (11, '粉玫瑰')；一个数量后面可能跟多种花。
_PAIR_RE = re.compile(r'(\d{1,3})\s*[朵枝支]\s*([^\d]{1,14})')

# ── 全网实测基线（2026-09-18，平台 1855 个在售商品 / 1435 条可解析）──────────
# 全部来自真实数据拟合，**没有一个是手写的经验值**：
#   [直测] 该花材有足够「纯单一花材商品」→ 单变量拟合（无共线性，最可信）
#   [校准] 其余花材 → 多元回归系数 × 校准系数（= 纯花材值 ÷ 多元值的中位数）
# 复现方式：``fit_from_rows(query 全量商品)``，见 tests/test_pricing.py。
_BASELINE_BASE_FEE = 46.0
_BASELINE_UNIT: dict[str, float] = {
    '玫瑰': 4.3,     # [直测] n=499, r=0.96
    '康乃馨': 2.6,   # [直测] n=90,  r=0.92
    '绣球': 17.4,    # [校准] 平台上最贵的花材
    '百合': 14.8,    # [校准]
    '郁金香': 11.3,  # [校准]
    '蝴蝶兰': 9.8,   # [校准]
    '向日葵': 9.6,   # [校准]
    '紫罗兰': 7.3,   # [校准]
    '洋桔梗': 4.3,   # [校准]
    '满天星': 4.2,   # [校准]
    '尤加利': 4.1,   # [校准]
    '洋甘菊': 3.5,   # [校准]
}
# 基线表里没有的花材 → 按价格档兜底（档位取自知识库 flowers.json）。
# 数值由上面的实测分布归纳：低档≈3.5、中档≈4.3~7.3、高档≈10~17。
_UNIT_BY_TIER: dict[str, float] = {'低': 4.0, '中': 6.0, '高': 13.0}
_UNIT_TIER_DEFAULT = 6.0

_CACHE: dict[str, tuple[float, 'PriceTable | None']] = {}
_LOCK = threading.Lock()
_TIER_CACHE: dict[str, str] = {}


# ── 数据结构 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PriceTable:
    """一份可用于报价的花材价格表（基础费 + 单支边际单价）。"""

    base_fee: float
    unit_prices: dict[str, float] = field(default_factory=dict)
    source: str = 'baseline'
    samples: int = 0
    shop_id: str = ''

    def unit_price(self, name: str) -> float:
        """取某花材的单支价；表里没有 → 按价格档兜底（绝不返回 0）。"""
        hit = self.unit_prices.get(str(name or '').strip())
        if hit and hit > 0:
            return float(hit)
        return _UNIT_BY_TIER.get(_tier_of(name), _UNIT_TIER_DEFAULT)

    def price_of(self, items: Iterable[tuple[str, int]]) -> float:
        """按两段式模型试算总价：``基础费 + Σ(支数 × 单价)``。"""
        total = float(self.base_fee)
        for name, qty in items:
            total += self.unit_price(name) * max(0, int(qty))
        return round(total)

    def describe(self) -> dict[str, Any]:
        """诊断信息（日志 / 排查用）。"""
        return {
            'source': self.source,
            'shop_id': self.shop_id,
            'base_fee': self.base_fee,
            'materials': len(self.unit_prices),
            'samples': self.samples,
        }


# ── 花材价格档（用于兜底）─────────────────────────────────────────────────

def _tier_of(name: str) -> str:
    """花材价格档（低/中/高）；知识库无记录时按「中」。结果带缓存。"""
    key = str(name or '').strip()
    if not key:
        return '中'
    hit = _TIER_CACHE.get(key)
    if hit:
        return hit
    tier = '中'
    try:
        from agent.knowledge import query_knowledge

        for f in query_knowledge('flower', key).get('results') or []:
            if str(f.get('name') or '') == key:
                tier = str(f.get('price_tier') or '中')
                break
    except Exception:  # noqa: BLE001 - 知识库不可用时按中档，不影响报价
        logger.debug('[pricing] 取花材档位失败：%s', key, exc_info=True)
    _TIER_CACHE[key] = tier
    return tier


# ── 配方解析 ──────────────────────────────────────────────────────────────

def _recipe_text(row: dict) -> str:
    """取用于解析配方的文本。

    ⚠️ **优先只用 ``flowers``，绝不与 name/subtitle 拼接** —— 实测很多商品的
    subtitle 与 flowers 内容一模一样，拼接后同一份配方会被解析两遍
    （``'11枝粉玫瑰花混搭'`` → ``[('玫瑰', 11), ('玫瑰', 11)]``），
    支数被重复计入、拟合结果全歪。只有 ``flowers`` 为空时才回退到名称。
    """
    flowers = row.get('flowers')
    if isinstance(flowers, list):
        flowers = ' '.join(str(x) for x in flowers)
    text = str(flowers or '').strip()
    if text:
        return text
    return ' '.join([str(row.get('name') or ''), str(row.get('subtitle') or '')]).strip()


def parse_recipe(text: str) -> list[tuple[str, int]]:
    """把配方文案解析成 ``[(花材规范名, 支数)]``。

    ``"11枝香槟玫瑰4朵向日葵6枝碎冰蓝混搭花束"`` → ``[('玫瑰',11), ('向日葵',4)]``
    （「碎冰蓝」不在知识库里，忽略 —— 宁可少算一种，也不要猜错价格）。

    一个数量后面跟多种花时（``"11枝紫玫瑰粉玫瑰"``）**平分支数**：
    没有更细的信息可用，平分是唯一不偏袒任何一方的处理。

    重复的 ``(花材, 支数)`` 对会被去重（防上游文本重复导致支数翻倍）。

    Args:
        text: 商品配方文案（``flowers`` 字段）。

    Returns:
        解析出的花材与支数；无法解析时返回空列表。
    """
    from agent.shop_materials import materials_in_text

    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for qty_s, name_txt in _PAIR_RE.findall(text or ''):
        qty = int(qty_s)
        names = sorted(materials_in_text(name_txt))
        if not names or qty <= 0:
            continue
        per = max(1, qty // len(names))
        for n in names:
            if (n, per) in seen:
                continue
            seen.add((n, per))
            out.append((n, per))
    return out


def _price_yuan(row: dict) -> float | None:
    """取商品售价（**元**）。

    ⚠️ **不要除 100**。平台原始接口的 ``price`` 确实是**分**，但经
    ``backend.data_gateway.http_source.normalize_row`` 规范化后**已经换算成元**
    （见该模块的 ``_cents_to_yuan``）；本模块只吃规范化后的行
    （``_fetch_rows`` 走的就是这条路）。

    曾经这里多除了一次 100 → 全表价格缩小 100 倍 → 拟合出的单价低于下限 →
    **所有店铺静默降级到全网基线**（表现为「锁店专属定价就是没生效」，极难排查）。
    配套防线见 :func:`fit_from_rows` 末尾的单位自检。
    """
    raw = row.get('price')
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    yuan = float(raw)
    return yuan if 0 < yuan < 100000 else None


# ── 拟合 ──────────────────────────────────────────────────────────────────

def _gauss(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """列主元高斯消元解线性方程组；奇异时返回 None。"""
    n = len(vector)
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(matrix[r][col]))
        if abs(matrix[pivot][col]) < 1e-9:
            return None
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        vector[col], vector[pivot] = vector[pivot], vector[col]
        for row in range(col + 1, n):
            factor = matrix[row][col] / matrix[col][col]
            if not factor:
                continue
            for c in range(col, n):
                matrix[row][c] -= factor * matrix[col][c]
            vector[row] -= factor * vector[col]
    out = [0.0] * n
    for row in range(n - 1, -1, -1):
        acc = vector[row] - sum(matrix[row][c] * out[c] for c in range(row + 1, n))
        out[row] = acc / matrix[row][row]
    return out


def _fit_single(pairs: list[tuple[float, int]]) -> tuple[float, float] | None:
    """单变量最小二乘 ``price = base + unit × qty``；至少要有 2 个不同支数点。"""
    if len({q for _, q in pairs}) < 2:
        return None
    n = len(pairs)
    sx = sum(q for _, q in pairs)
    sy = sum(p for p, _ in pairs)
    sxx = sum(q * q for _, q in pairs)
    sxy = sum(p * q for p, q in pairs)
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        return None
    unit = (n * sxy - sx * sy) / den
    return (sy - unit * sx) / n, unit


def _multi_fit(samples: list[tuple[float, list[tuple[str, int]]]]
               ) -> tuple[float, dict[str, float]] | None:
    """多元岭回归解 ``price ≈ base + Σ(支数_i × 单价_i)``。

    ⚠️ 它有个**已知偏差**：玫瑰几乎总和别的花同框出现（共线性），
    加上岭正则会把它往 0 收缩 —— 实测玫瑰被压到 2.0 元，而纯花材款的
    真实值是 4.3 元。所以它的**绝对值不可直接采信**，只用来：
    ① 覆盖没有纯花材数据的其他花材；② 反映「这家店整体比别家贵/便宜」。
    绝对值由 :func:`fit_price_table` 用纯花材结果校准。
    """
    mats = sorted({m for _, rec in samples for m, _ in rec})
    if not mats:
        return None
    idx = {m: i for i, m in enumerate(mats)}
    size = len(mats) + 1                      # 末位是截距（基础费）
    mat = [[0.0] * size for _ in range(size)]
    vec = [0.0] * size
    used = 0
    for price, rec in samples:
        x = [0.0] * size
        for name, qty in rec:
            x[idx[name]] += qty
        if not any(x[:size - 1]):
            continue                          # 该行没有可用花材
        used += 1
        x[size - 1] = 1.0
        for i in range(size):
            if not x[i]:
                continue
            for j in range(size):
                mat[i][j] += x[i] * x[j]
            vec[i] += x[i] * price
    if used < _MIN_SAMPLES:
        return None
    for i in range(size - 1):                 # 截距不正则化
        mat[i][i] += _RIDGE
    theta = _gauss(mat, vec)
    if theta is None:
        return None
    units = {m: theta[idx[m]] for m in mats if theta[idx[m]] > 0}
    base = theta[size - 1]
    if not math.isfinite(base) or not 0 <= base <= _MAX_BASE:
        base = _BASELINE_BASE_FEE
    return float(base), units


def fit_price_table(samples: list[tuple[float, list[tuple[str, int]]]],
                    shop_id: str = '') -> PriceTable | None:
    """拟合价表：**纯花材款定绝对值，多元回归补覆盖面**。

    两步走的原因（实测踩过）：
    1. **纯单一花材商品**（「11枝玫瑰 88元」）没有共线性，单变量拟合出的单价
       就是真实水平（玫瑰 4.3 元/支，r=0.96）——这部分**直接用**；
    2. **多元回归**能覆盖所有花材、也能反映店铺间差异，但绝对值被共线性压低
       （玫瑰只解出 2.0）—— 所以乘一个校准系数（纯花材值 ÷ 多元值的中位数）
       把整体水平校回来。

    Args:
        samples: ``[(售价元, [(花材, 支数)])]``。
        shop_id: 用于标注来源。

    Returns:
        价表；样本不足或解不可信时返回 None（调用方降级）。
    """
    if len(samples) < _MIN_SAMPLES:
        return None

    # ① 纯单一花材款：无共线性，绝对值最可信
    pure: dict[str, list[tuple[float, int]]] = {}
    for price, rec in samples:
        if len(rec) == 1:
            name, qty = rec[0]
            pure.setdefault(name, []).append((price, qty))
    pure_units: dict[str, float] = {}
    pure_bases: list[float] = []
    for name, pairs in pure.items():
        got = _fit_single(pairs)
        if got is None:
            continue                          # 支数点不足（如全是 11 支）→ 不用它定值
        base, unit = got
        if _MIN_UNIT <= unit <= _MAX_UNIT and 0 <= base <= _MAX_BASE:
            pure_units[name] = round(unit, 1)
            pure_bases.append(base)

    # ② 多元回归：补其它花材 + 反映该店整体水位
    multi = _multi_fit(samples)
    if multi is None and not pure_units:
        return None
    multi_base, multi_units = multi if multi else (_BASELINE_BASE_FEE, {})

    # ③ 校准系数：把多元的绝对水平拉回纯花材实测水平
    ratios = [pure_units[n] / multi_units[n]
              for n in pure_units if multi_units.get(n, 0) > 0]
    calib = sorted(ratios)[len(ratios) // 2] if ratios else 1.0
    calib = min(max(calib, 0.5), 3.0)         # 防极端值把整表带偏

    # ④ 合并：纯花材值优先，其余用校准后的多元值
    units: dict[str, float] = dict(pure_units)
    for name, value in multi_units.items():
        if name in units:
            continue
        scaled = value * calib
        if scaled >= _MIN_UNIT:
            units[name] = round(min(scaled, _MAX_UNIT), 1)
    if not units:
        return None

    base = sorted(pure_bases)[len(pure_bases) // 2] if pure_bases else multi_base
    logger.info('[pricing] shop=%s 拟合成功：样本 %d、花材 %d 种（纯花材定值 %d 种）、'
                '基础费 %.0f 元、校准系数 %.2f',
                shop_id or '(全网)', len(samples), len(units), len(pure_units), base, calib)
    return PriceTable(base_fee=round(float(base), 1), unit_prices=units,
                      source='merchant' if shop_id else 'baseline',
                      samples=len(samples), shop_id=shop_id)


def fit_from_rows(rows: list[dict], shop_id: str = '') -> PriceTable | None:
    """从平台商品行拟定价表（过滤脏数据后调用 :func:`fit_price_table`）。

    ⚠️ 传入的必须是经 ``normalize_row`` **规范化后**的行（``price`` 单位是**元**，
    不是平台原始接口的「分」）。见 :func:`_price_yuan`。
    """
    samples: list[tuple[float, list[tuple[str, int]]]] = []
    skipped = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        price = _price_yuan(row)
        recipe = parse_recipe(_recipe_text(row))
        if price is None or not recipe:
            skipped += 1
            continue
        samples.append((price, recipe))
    logger.info('[pricing] shop=%s 商品 %d 条 → 可解析 %d 条（跳过 %d）',
                shop_id or '(全网)', len(rows or []), len(samples), skipped)
    # 单位自检：平台最便宜的在售商品也在 20 元以上。中位价低于 _MIN_SANE_PRICE
    # 基本可以确定是「把元当成分又除了一次」这类单位错误 —— 它的表现是
    # 「所有店铺都静默降级到基线」，不报错很难查，所以这里直接留痕并放弃拟合。
    if samples:
        prices = sorted(p for p, _ in samples)
        median = prices[len(prices) // 2]
        if median < _MIN_SANE_PRICE:
            logger.error('[pricing] shop=%s 商品价格中位数仅 %.2f 元，疑似价格单位错误'
                         '（原始接口是「分」，规范化后应为「元」）→ 放弃拟合、降级基线',
                         shop_id or '(全网)', median)
            return None
    return fit_price_table(samples, shop_id)


# ── 取数 ──────────────────────────────────────────────────────────────────

def _source_ids() -> list[str]:
    """已配置的平台数据源（延迟导入，规避与 agent.agent 的循环依赖）。"""
    try:
        from agent.agent import _platform_source_ids

        return _platform_source_ids()
    except Exception:  # noqa: BLE001 - 取不到就当作未配置
        return []


def _fetch_rows(shop_id: str) -> list[dict] | None:
    """拉该店（或全平台）在售商品；失败返回 None（未知，由调用方降级）。"""
    from backend.data_gateway.external import query_external_entity

    sources = _source_ids()
    if not sources:
        return None
    best: list[dict] | None = None
    for sid in sources:
        try:
            rows = query_external_entity(sid, 'plan', limit=_LIMIT, shop_id=shop_id)
        except Exception:  # noqa: BLE001 - 单个数据源失败不影响其它源
            logger.warning('[pricing] 拉取商品失败（source=%s shop=%s）', sid, shop_id or '-',
                           exc_info=True)
            continue
        if rows and (best is None or len(rows) > len(best)):
            best = rows
    return best


# ── 商家自定义价表（预留）───────────────────────────────────────────────

def load_merchant_table(shop_id: str) -> PriceTable | None:
    """商家自定义价表 —— **为将来的商家工作台预留**。

    Capri 2026-09-18：「后面可能会给商家出一个工作台，商家可以在工作台上
    设定标准价格」。届时应由平台下发，优先级**高于一切自动推算**（商家说了算）。

    现在没有任何来源，恒返回 None，定价自动降级到「该店商品实测」。
    """
    return None


# ── 主入口 ────────────────────────────────────────────────────────────────

def baseline_table(shop_id: str = '') -> PriceTable:
    """全网实测基线表（内置，无需取数）。"""
    return PriceTable(base_fee=_BASELINE_BASE_FEE,
                      unit_prices=dict(_BASELINE_UNIT),
                      source='baseline', samples=0, shop_id=shop_id)


def price_table(shop_id: str = '') -> PriceTable:
    """取该店（或全网）的价表 —— **报价的唯一入口**。

    降级顺序：商家自定义 → 该店商品实测 → 全网基线。
    任何一步失败都不会抛错（报价是主流程，不能因为取数失败就崩）。

    Args:
        shop_id: 店铺 ID；留空走基线。

    Returns:
        可直接用于报价的 :class:`PriceTable`（永远有值）。
    """
    sid = str(shop_id or '').strip()
    custom = load_merchant_table(sid)
    if custom is not None:
        return custom
    if sid:
        fitted = _fit_cached(sid)
        if fitted is not None:
            return fitted
    return baseline_table(sid)


def _fit_cached(shop_id: str) -> PriceTable | None:
    """带缓存的该店拟合（失败结果也缓存，避免每次都去拉一遍）。"""
    from backend.data_gateway.access import cache_scope
    scope = cache_scope()
    key = shop_id if scope is None else (scope, shop_id)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    with _LOCK:
        hit = _CACHE.get(key)              # 双检：并发下只让一个线程真去拉
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
        table: PriceTable | None = None
        rows = _fetch_rows(shop_id)
        if rows:
            table = fit_from_rows(rows, shop_id)
        if table is None:
            logger.info('[pricing] 店铺 %s 样本不足或取数失败 → 降级到全网基线', shop_id)
        _CACHE[key] = (time.time(), table)
        return table


def clear_cache(shop_id: str | None = None) -> None:
    """清缓存（测试与商家改价后使用）。"""
    with _LOCK:
        if shop_id is None:
            _CACHE.clear()
        else:
            sid = str(shop_id).strip()
            for key in list(_CACHE):
                if key == sid or (isinstance(key, tuple) and key[1] == sid):
                    _CACHE.pop(key, None)
