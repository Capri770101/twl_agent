"""官网演示页（``www.tiaowulan.com/agent.html``）适配层：内部方案 → 页面契约。

页面契约（由 `agent-client.js` 的 live 分支与 `main.js` 的渲染代码实测确定）::

    POST {endpoint}   body: {input, parsed:{recipient,scene,style,budget}}
    → {success, mode:'live', requestId,
       parsed:{recipient,scene,style,budget},
       plan:{title, tier,
             flowers:[{name,count,meaning}],
             palette:[{name,hex}],
             language, craft,
             pricing:{suggested,materialCost,margin,marginRate,stems,
                      withinBudget,warning,reason},
             image, note},
       note}

与页面渲染相关的**三条硬约束**（读 main.js 得出，改动前务必复核）：

1. 色板必须带 ``hex``（渲染成 ``<i style="background:hex">``），色名只有标题提示作用；
2. 花材名不能自带支数（页面渲染成 ``name ×count``，自带支数会变成「康乃馨 ×11 ×11」）；
3. 底部说明读的是 **``plan.note``**（``pl.note || pr.reason``）——顶层 ``note`` 页面并不读，
   官网 demo 自己的顶层 ``note`` 其实从未显示过。

定价口径（2026-09-18 变更，Capri 拍板）：**报价 = 花材零售额 + 基础费（包装与手工）**。
花材单价取自 `agent/pricing`（从平台在售商品反推的**零售价**），所以本模块
**不再叠加任何「成本 × 加价率」的保底** —— 那等于对零售价二次加价（实测会把
195 元的方案抬到 201 元、直接顶破预算）。「不亏本」这个原始目标已由
「直接采用平台在售价」自动达成（平台商家本来就是按这个价在卖）。

当配置本身压不进用户预算时，如实给出 ``pricing.warning``（页面用强调色渲染）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import uuid
from typing import Any

from agent.tools import _build_plan, extract_requirement

logger = logging.getLogger('agent.client_payload')

# ── 页面档位（沿用官网 demo 的 5 档命名，避免同一页面出现两套说法）──
# ⚠️ 档位标签按**最终售价**归属（而非用户预算）：否则会出现「标准优选 · 380 元」这种自相矛盾的展示。
PAGE_TIERS: tuple[tuple[float, str, float], ...] = (
    (150, '简约心意', 1.6),
    (300, '标准优选', 1.8),
    (500, '品质甄选', 2.0),
    (900, '尊享定制', 2.2),
    (float('inf'), '高定礼遇', 2.5),
)

ROUND_STEP = 10        # 报价取整到 10 元
MAX_PALETTE = 4        # 页面色板最多展示 4 个色块

# **显式**枝数（「11朵」「十一支」「两朵」…）。
# 刻意**不含**裸「一束 / 一打」——那两个是启发式默认值（一束=11、一打=12），
# 用户同时给出了预算时应让预算说了算：否则「送妈妈一束花，预算200」会解析成
# 硬性 11 支 → 材料成本 276 元、报价 318 元，超预算 59%。
_EXPLICIT_STEMS = re.compile(
    r'(\d{1,3}|[二两三四五六七八九]|十[一二三四五六七八九]?|[一二三四五六七八九]十[一二三四五六七八九]?)'
    r'\s*(?:朵|支|枝|束|根|头)'
)

_DEFAULT_PALETTE: list[dict[str, str]] = [{'name': '自然色系', 'hex': '#DCD6C8'}]
_DEFAULT_SCENE = '日常赠礼'
_DEFAULT_STYLE = '经典'

_COLOR_CACHE: dict[str, str] | None = None


# ── 色名 → hex ─────────────────────────────────────────────────────────────

def color_map() -> dict[str, str]:
    """加载「色名 → hex」表（`agent/knowledge/colors.json`），失败时退化为空表。"""
    global _COLOR_CACHE
    if _COLOR_CACHE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'knowledge', 'colors.json')
        try:
            with open(path, encoding='utf-8') as handle:
                rows = json.load(handle)
            _COLOR_CACHE = {
                str(r['name']): str(r['hex'])
                for r in rows
                if isinstance(r, dict) and r.get('name') and r.get('hex')
            }
        except Exception:  # noqa: BLE001
            logger.warning('[client_payload] colors.json 读取失败，色板退化为默认色', exc_info=True)
            _COLOR_CACHE = {}
    return _COLOR_CACHE


def resolve_palette(names: Any, limit: int = MAX_PALETTE) -> list[dict[str, str]]:
    """色名列表 → 页面色板 ``[{name, hex}]``（未收录的色名丢弃，全丢则给默认色）。

    Args:
        names: 色名（字符串或列表）；知识库里的 ``color_scheme`` 可能是列表或「粉/白」字符串。
        limit: 最多返回几个色块。

    Returns:
        至少一项的色板列表——页面会直接渲染 ``background:hex``，不能为空。
    """
    if isinstance(names, str):
        items = re.split(r'[、，,/\s+·|]+', names)
    elif isinstance(names, (list, tuple)):
        items = list(names)
    else:
        items = []
    cmap = color_map()
    out: list[dict[str, str]] = []
    seen_hex: set[str] = set()
    for raw in items:
        key = str(raw or '').strip()
        if not key or any(o['name'] == key for o in out):
            continue
        hexv = cmap.get(key)
        # 同色值只留一个色块：知识库的色名有近义项（红/正红 → 同一 hex），
        # 重复渲染会让用户以为方案里有两种一样的颜色。
        if not hexv or hexv in seen_hex:
            continue
        seen_hex.add(hexv)
        out.append({'name': key, 'hex': hexv})
        if len(out) >= limit:
            break
    return out or [dict(_DEFAULT_PALETTE[0])]


# ── 档位 ────────────────────────────────────────────────────────────────────

def page_tier(price: float | int | None) -> tuple[str, float]:
    """按**最终售价**取页面档位标签与加价系数。

    Args:
        price: 建议售价（元）。

    Returns:
        ``(档位标签, 加价系数)``。
    """
    value = float(price or 0)
    for upper, label, ratio in PAGE_TIERS:
        if value <= upper:
            return label, ratio
    last = PAGE_TIERS[-1]
    return last[1], last[2]


# ── 方案 → 页面字段 ─────────────────────────────────────────────────────────

def _flower_groups(design: dict) -> list[dict]:
    """按 主花 → 配材 → 叶材 顺序拉平花材列表。"""
    if not isinstance(design, dict):
        return []
    out: list[dict] = []
    for key in ('main_flowers', 'fillers', 'foliage'):
        for item in design.get(key) or []:
            if isinstance(item, dict) and item.get('name'):
                out.append(item)
    return out


def material_cost(design: dict) -> int:
    """花材成本（Σ 支数 × 单价）——**不含**包装/人工/装饰，与页面 ``materialCost`` 口径一致。"""
    return int(sum((f.get('qty') or 0) * (f.get('unit_price') or 0) for f in _flower_groups(design)))


def total_stems(design: dict) -> int:
    """总枝数。"""
    return int(sum(f.get('qty') or 0 for f in _flower_groups(design)))


def _meaning_of(name: str) -> str:
    """查知识库取花语（取前 3 个词），查不到返回空串。"""
    try:
        from agent.tools import _known_flower  # 复用别名匹配，避免两套查表

        data = _known_flower(name) or {}
        langs = data.get('flower_language') or []
        return '、'.join(str(x) for x in langs[:3])
    except Exception:  # noqa: BLE001
        return ''


def build_flowers(design: dict) -> list[dict[str, Any]]:
    """花材列表 → 页面 ``flowers``（name 必须**不带**支数，页面自己会拼 ``name ×count``）。"""
    out: list[dict[str, Any]] = []
    for item in _flower_groups(design):
        name = re.sub(r'\s*[×xX*]\s*\d+\s*$', '', str(item.get('name') or '').strip()).strip()
        if not name:
            continue
        out.append({
            'name': name,
            'count': int(item.get('qty') or 0),
            'meaning': _meaning_of(name),
        })
    return out


def _craft_text(design: dict, steps: Any) -> str:
    """工艺描述：包装器型 + DIY 步骤里的「包装收尾」细节（去掉重复引用包装名与括注）。"""
    pkg = str(design.get('packaging') or '花束').strip()
    step = ''
    if isinstance(steps, list):
        step = next((str(s) for s in steps if str(s).startswith('5.')), '')
    detail = re.sub(r'^5\.\s*包装收尾：', '', step)
    detail = re.sub(r'用「[^」]*」', '', detail)   # 「用「花束（螺旋扎）」（…）」与 pkg 重复
    detail = re.sub(r'（[^）]*）', '', detail)
    detail = detail.strip('，。、 ')
    return f'{pkg}；{detail}' if detail else pkg


def _language_text(plan: dict, design: dict) -> str:
    """页面「花语」：优先贺卡文案，其次花语词。"""
    card = str(plan.get('card_message') or '').strip()
    if card:
        return card
    return str(design.get('meaning') or '').strip() or '以花传情'


def compute_pricing(plan: dict) -> dict[str, Any]:
    """按页面口径计算定价。

    ⚠️ **口径变更（2026-09-18 定价改造，必读）**：
    ``unit_price`` 过去是代码里写死的「花材成本参考价」（12/28/60），
    现在改为**从平台在售商品反推的零售价**（见 :mod:`agent.pricing`）。
    因此这里**不再叠加任何「成本 × 加价率」的保底**（原为 ×1.35）—— 那等于给零售价
    再加一次毛利，实测会把一束 195 元的方案抬到 201 元、直接顶破用户预算
    （页面上表现为「预算 200 却提示超预算」）。
    保底的含义随之改为：**报价不低于花材自身的零售金额**（即不打折卖花材），
    而「不亏本」这个原始目标已由「直接采用平台在售价」自动达成
    —— 平台商家本来就是按这个价在卖。
    （Capri 2026-09-18 确认：不再保留 1.35 的对外承诺。）

    Args:
        plan: ``_build_plan`` 产出的方案（含 ``design`` 与 ``budget_breakdown``）。

    Returns:
        页面 ``pricing`` 字段。
    """
    design = plan.get('design') or {}
    breakdown = plan.get('budget_breakdown') or {}
    cost = material_cost(design)
    stems = total_stems(design)
    quoted = int(breakdown.get('total_estimate') or 0)
    floor = int(math.ceil(cost / ROUND_STEP) * ROUND_STEP) if cost else 0
    suggested = max(quoted, floor)
    material = max(0, suggested - cost)          # 包装、手工与基础服务部分
    material_rate = round(material / suggested * 100) if suggested else 0

    budget = plan.get('budget_num')
    budget = int(budget) if isinstance(budget, (int, float)) and budget else 0

    warning: str | None = None
    if quoted and floor and quoted < floor:
        # 报价低于花材零售合计 → 抬到花材价（不能把花材本身打折卖）
        warning = f'已按花材零售价 {suggested} 元测算（花材 {cost} 元）。'
    elif budget and suggested > budget:
        warning = (f'当前预算偏低：按标准配置建议售价 {suggested} 元（花材 {cost} 元 + '
                   f'包装与手工 {material} 元）。')

    reason = (
        f'{stems} 枝花材约 {cost} 元，加包装与手工 {material} 元，'
        f'建议售价 {suggested} 元（其中包装与手工占 {material_rate}%）。'
        '价格按平台在售价推算，实际以门店确认为准。'
    )
    return {
        'suggested': suggested,
        'materialCost': cost,
        'margin': material,
        'marginRate': material_rate,
        'stems': stems,
        'withinBudget': (suggested <= budget) if budget else None,
        'warning': warning,
        'reason': reason,
    }


# ── 需求 → 方案（含「启发式枝数让位预算」规则）──────────────────────────────

def build_requirements(plan_dims: dict[str, str]) -> dict[str, str]:
    """把用户原始输入 + 页面已解析的四要素合并成一维需求（只补空，不覆盖）。"""
    return {k: v for k, v in (plan_dims or {}).items() if v}


def design_plan(text: str, parsed: dict[str, Any] | None = None) -> dict:
    """文本 → 结构化方案（纯规则引擎，无 LLM 调用：快、免费、结果稳定）。

    两条与核心不同的处理，都为了「预算可信」：

    1. 合并页面已解析的四要素（它自己抽过一轮，别浪费）；
    2. **启发式枝数让位预算**：用户只说了「一束」而没给明确枝数时，丢掉启发式的
       ``stem_count``，让 ``_alloc_stems`` 的预算约束生效（否则 200 元预算会产出 318 元方案）。

    Args:
        text: 用户原始输入。
        parsed: 页面解析出的 ``{recipient, scene, style, budget}``。

    Returns:
        ``_build_plan`` 产出的方案 dict。
    """
    parsed = parsed or {}
    chunks = [str(text or '').strip()]
    for key in ('recipient', 'scene', 'style'):
        value = str(parsed.get(key) or '').strip()
        # 「未指定」是页面自己的占位值，不能当需求喂进来
        if value and value not in ('未指定', '—', '-'):
            chunks.append(value)
    budget = parsed.get('budget')
    if isinstance(budget, (int, float)) and budget:
        chunks.append(f'预算{int(budget)}')
    merged = ' '.join(c for c in chunks if c)

    dims = dict(extract_requirement(merged).to_legacy_dict())
    if dims.get('budget') and not _EXPLICIT_STEMS.search(merged):
        # 只有「一束/一打」这类启发式数量时，让预算决定枝数
        dims.pop('stem_count', None)
    return _build_plan(dims)


def build_client_payload(text: str, parsed: dict[str, Any] | None = None, *, note: str = '') -> dict:
    """顶层入口：用户输入 → 页面契约响应的 ``plan`` 部分（含 pricing/palette/note）。

    Args:
        text: 用户原始输入。
        parsed: 页面解析出的四要素。
        note: 附加说明（会写进 **``plan.note``**，因为页面只读 ``plan.note``）。

    Returns:
        页面可直接渲染的响应 dict（``success`` / ``mode`` / ``parsed`` / ``plan``）。
    """
    parsed = parsed or {}
    plan = design_plan(text, parsed)
    design = plan.get('design') or {}
    pricing = compute_pricing(plan)
    label, _ratio = page_tier(pricing['suggested'])

    req = extract_requirement(' '.join(filter(None, [
        str(text or ''),
        str(parsed.get('recipient') or ''),
        str(parsed.get('scene') or ''),
        str(parsed.get('style') or ''),
    ])))
    budget = parsed.get('budget') if isinstance(parsed.get('budget'), (int, float)) else 0

    notes: list[str] = []
    if note:
        notes.append(note)
    if not req.recipient and not req.occasion and req.budget_num is None:
        notes.append('未指定送花对象与场合，已按通用场景生成；想更贴合，可以说一句：送给谁、什么场合、预算多少。')

    return {
        'success': True,
        'mode': 'live',
        'requestId': 'live_' + uuid.uuid4().hex[:12],
        'parsed': {
            'recipient': req.recipient or str(parsed.get('recipient') or '') or '未指定',
            'scene': str(parsed.get('scene') or '') or (plan.get('scene') or _DEFAULT_SCENE),
            'style': plan.get('style') or str(parsed.get('style') or '') or _DEFAULT_STYLE,
            'budget': int(budget or req.budget_num or 0),
        },
        'plan': {
            'title': str(plan.get('name') or '定制花束'),
            'tier': label,
            'flowers': build_flowers(design),
            'palette': resolve_palette(design.get('color_scheme')),
            'language': _language_text(plan, design),
            'craft': _craft_text(design, plan.get('diy_steps')),
            'pricing': pricing,
            'image': None,  # 效果图一期不接：页面渲染层目前不使用该字段，且生图需异步轮询
            'note': ' '.join(notes),
        },
        'note': '',  # 顶层 note 页面不读，保留字段以免契约缺项
    }
