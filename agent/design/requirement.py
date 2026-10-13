"""需求理解规则层：关键词表、场景/风格索引、花名表、需求抽取与合并。

只做规则匹配与知识库查询，不调用 LLM、不生成方案。
DIY 设计、方案检索、店铺检索共用这套口径，避免散落多处。
"""
from __future__ import annotations

import logging
import re

from agent.design.quantities import (
    _AMOUNT_PAT, _MAGNITUDE_UNITS, _cn_to_int, _extract_budget,
    _extract_stem_count, _safe_float, _safe_int,
)
from agent.design.gift_intent import INTENT_LANGUAGE, understand_gift_message
from agent.knowledge import get_by_id, query_knowledge
from domain.requirements import FlowerRequirement

logger = logging.getLogger('tools')

_RECIPIENT_KW = {'妈妈': '母亲', '母亲': '母亲', '妈': '母亲', '娘': '母亲', '恋人': '恋人', '女朋友': '恋人', '男朋友': '恋人', '老婆': '恋人', '老公': '恋人', '对象': '恋人', '爱人': '恋人', '男友': '恋人', '女友': '恋人', '先生': '恋人', '丈夫': '恋人', '朋友': '朋友', '闺蜜': '朋友', '兄弟': '朋友', '同事': '朋友', '姐妹': '朋友', '自己': '自己', '悦己': '自己', '我': '自己', '长辈': '长辈', '老人': '长辈', '父母': '长辈', '领导': '长辈', '上司': '长辈', '老板': '长辈', '老师': '长辈', '宝宝': '宝宝', '婴儿': '宝宝', '新生儿': '宝宝'}
_OCCASION_KW = {'生日': '生日', '庆祝': '生日', '母亲节': '母亲', '父亲节': '父亲', '节': '节日', '告白': '告白', '表白': '告白', '纪念日': '告白', '求婚': '告白', '婚礼': '婚礼', '结婚': '婚礼', '领证': '婚礼', '探病': '探病', '生病': '探病', '康复': '探病', '住院': '探病', '道歉': '道歉', '对不起': '道歉', '抱歉': '道歉', '毕业': '毕业', '乔迁': '乔迁', '开业': '开业', '升职': '升职', '入职': '入职'}
_STYLE_KW = {'韩式': 'S_KOREAN', '韩系': 'S_KOREAN', '北欧': 'S_NORDIC', '简约': 'S_NORDIC', '极简': 'S_NORDIC', '复古': 'S_VINTAGE', '古典': 'S_VINTAGE', '港风': 'S_VINTAGE', '中古': 'S_VINTAGE', '自然': 'S_NATURAL', '野趣': 'S_NATURAL', '田园': 'S_NATURAL', 'ins': 'S_INS', 'ins风': 'S_INS', '网红': 'S_INS', '奶油风': 'S_INS', '法式': 'S_INS', '日式': 'S_JAPANESE', '禅': 'S_JAPANESE', '日系': 'S_JAPANESE'}
_COLOR_KW = {'红': '红', '粉': '粉', '白': '白', '香槟': '香槟', '紫': '紫', '蓝': '蓝', '黄': '黄', '橙': '橙', '绿': '绿', '多彩': '多彩混合', '亮': '亮', '鲜艳': '亮', '缤纷': '多彩混合', '粉嫩': '粉', '浅粉': '粉', '桃红': '粉', '正红': '红', '酒红': '红', '橘': '橙', '鹅黄': '黄', '天蓝': '蓝', '湖蓝': '蓝', '香槟色': '香槟', '五彩': '多彩混合', '撞色': '多彩混合'}
_MOOD_KW = {'温柔': '温柔', '温馨': '温馨', '浪漫': '浪漫', '清新': '清新', '热烈': '热烈', '活泼': '活泼', '高级': '高级', '素雅': '素雅', '优雅': '优雅', '莫兰迪': '素雅', '马卡龙': '清新', '小清新': '清新', '轻奢': '高级', '低调': '素雅', '治愈': '治愈', '安静': '素雅', '甜美': '甜美', '氛围感': '优雅', '高级感': '高级'}
_RELATIONSHIP_MAP = {'母亲': '亲子', '恋人': '情侣', '朋友': '朋友', '自己': '自用', '长辈': '长辈/同事', '宝宝': '亲子'}


def _build_scene_map() -> dict[str, str]:
    m: dict[str, str] = {}
    for s in query_knowledge('scene', '')['results']:
        for kw in s.get('keywords', []):
            m[kw] = s['id']
    return m
_SCENE_MAP: dict[str, str] | None = None

def _get_scene_map() -> dict[str, str]:
    global _SCENE_MAP
    if _SCENE_MAP is None:
        _SCENE_MAP = _build_scene_map()
    return _SCENE_MAP

def _get_style_full(style_id: str | None) -> tuple[dict | None, dict | None]:
    """解析风格（含子风格）：返回 (resolved_style, parent_style)。

    先查顶层风格；再查各风格的 substyles；找不到返回 (None, None)。
    resolved_style 用于取 typical_flowers/color_palette/packaging/vibe；
    parent_style 在子风格缺字段时回退。
    """
    if not style_id:
        return (None, None)
    top = get_by_id('style', style_id)
    if top:
        return (top, top)
    for parent in query_knowledge('style', '')['results']:
        for sub in parent.get('substyles', []):
            if sub['id'] == style_id:
                return (sub, parent)
    return (None, None)
_STYLE_INDEX: tuple[dict, dict] | None = None

def _get_style_index() -> tuple[dict, dict]:
    """构建「风格名 -> 标识」的反查索引（顶层 + 子风格），模块级缓存。

    返回 (top_map, sub_map)，value 为 (style_id, substyle_id, substyle_name)。
    供 _match_style 把 LLM 自由生成的 style 名锚定回知识库一致 id。
    """
    global _STYLE_INDEX
    if _STYLE_INDEX is None:
        top, sub = ({}, {})
        for s in query_knowledge('style', '')['results']:
            top[s['name']] = (s['id'], None, None)
            for sub_style in s.get('substyles', []):
                sub[sub_style['name']] = (s['id'], sub_style['id'], sub_style['name'])
        _STYLE_INDEX = (top, sub)
    return _STYLE_INDEX

def _match_style(style_name: str | None) -> tuple[str | None, str | None, str | None]:
    """按风格名（容忍『北欧风/自然系』后缀差异）反查知识库，返回 (style_id, substyle_id, substyle_name)。

    用途：LLM 自由生成 style 名（如『自然风』），需锚定回知识库一致的 style_id，
    消除『style 名与 style_id 错位』。优先匹配顶层风格，再退到子风格。
    """
    if not style_name:
        return (None, None, None)
    raw = style_name.strip()
    norm = raw.rstrip('风系感').strip()
    top, sub = _get_style_index()

    def _lookup(mapping: dict) -> tuple | None:
        if raw in mapping:
            return mapping[raw]
        if norm and norm in mapping:
            return mapping[norm]
        for key, val in mapping.items():
            if not key:
                continue
            if raw in key or key in raw or (norm and (norm in key or key in norm)):
                return val
        return None
    hit = _lookup(top)
    if hit:
        return hit
    hit = _lookup(sub)
    return hit or (None, None, None)
_ALL_FLOWER_NAMES: list[str] | None = None

def _get_all_flower_names() -> list[str]:
    global _ALL_FLOWER_NAMES
    if _ALL_FLOWER_NAMES is None:
        _ALL_FLOWER_NAMES = [f['name'] for f in query_knowledge('flower', '')['results']]
    return _ALL_FLOWER_NAMES

def _infer_substyle(style_id: str, dims: dict[str, str]) -> str | None:
    """未由场景指定子风格时，按情感/氛围从粗风格推导细分。"""
    mood = dims.get('mood', '')
    if style_id == 'S_KOREAN':
        return 'S_KOREAN_LUXE' if mood in ('高级', '克制') else 'S_KOREAN_SWEET'
    if style_id == 'S_NORDIC':
        return 'S_NORDIC_MINIMAL' if mood in ('极简', '文艺', '素雅') else 'S_NORDIC_PASTORAL'
    if style_id == 'S_VINTAGE':
        return 'S_VINTAGE_HK' if mood in ('港风', '怀旧', '浓烈') else 'S_VINTAGE_OIL'
    if style_id == 'S_NATURAL':
        return 'S_NATURAL_FOREST' if mood in ('森系', '治愈', '安静') else 'S_NATURAL_WILD'
    if style_id == 'S_INS':
        return 'S_INS_POP' if mood in ('撞色', '活泼', '年轻', '打卡') else 'S_INS_CREAM'
    if style_id == 'S_JAPANESE':
        return 'S_JAPANESE_SEASON' if mood in ('季节', '情绪') else 'S_JAPANESE_MINIMAL'
    if style_id == 'S_FRENCH':
        return 'S_FRENCH_ELEGANT' if mood in ('高级', '克制', '优雅') else 'S_FRENCH_GARDEN'
    if style_id == 'S_AMERICAN':
        return 'S_AMERICAN_RETRO' if mood in ('复古', '浓烈', '撞色') else 'S_AMERICAN_COUNTRY'
    if style_id == 'S_BOHO':
        return 'S_BOHO_COLOR' if mood in ('撞色', '活泼', '奔放') else 'S_BOHO_WILD'
    if style_id == 'S_LUXE':
        return 'S_LUXE_WHITE' if mood in ('白', '干净', '冷静') else 'S_LUXE_CHAMPAGNE'
    if style_id == 'S_MORANDI':
        return 'S_MORANDI_BLUE' if mood in ('蓝', '冷静', '文艺') else 'S_MORANDI_PINK'
    if style_id == 'S_COLOR':
        return 'S_COLOR_GRADIENT' if mood in ('渐变', '梦幻', '多彩') else 'S_COLOR_ENERGY'
    return None

_ALL_FLOWER_TERMS: list[str] | None = None

def _all_flower_terms() -> list[str]:
    """所有花名 + 别名（长词优先），用于『纯红玫瑰』这类含别名的单一花材识别。"""
    global _ALL_FLOWER_TERMS
    if _ALL_FLOWER_TERMS is None:
        terms: list[str] = []
        for f in query_knowledge('flower', '')['results']:
            terms.append(f['name'])
            terms.extend(f.get('aliases', []))
        # 长词优先，避免『玫瑰』先命中而漏掉『红玫瑰』
        _ALL_FLOWER_TERMS = sorted(set(terms), key=len, reverse=True)
    return _ALL_FLOWER_TERMS

# 连接词：出现即视为多花材混搭，不应判为单一花材（避免「红玫瑰11朵和百合」被误判）。
_CONNECTIVE_WORDS = ('搭配', '配', '加', '和', '还有', '以及', '再加', '混搭', '组合',
                     '与', '另外', '顺带', '附带', '点缀', '来点', '再来', '其它', '其他')

def _detect_single_flower(text: str) -> str | None:
    """检测单一花材意图，返回指定花名；无此意图返回 None。

    命中规则（任一）：
    1. 限定词 + 花名：纯/只要/仅用/单一花材/only + <花名>
    2. 花名 + 收束词：<花名> + 一束/就好/即可/就行/单色
    3. 花名 + 数量 + 单位（朵/支/枝/束），且全文仅含这一种花、无其他花或连接词：
       例「红玫瑰11朵」「白玫瑰9支」「百合一束」。避免「红玫瑰11朵百合5朵」被误判。

    由于必须匹配到已知花名，『纯色系』『同色系』等纯配色表述（不含花名）不会被误判。
    """
    # 规则 1 & 2：限定词 / 收束词
    # 限定词与花名之间允许夹一个颜色字（「纯白百合」「只要粉玫瑰」）——中文里这种夹色写法
    # 极常见，而花名表用的是通用名（百合 / 玫瑰），不放行就会漏判。
    _color_gap = r'(?:[红粉白香槟紫蓝黄橙绿]{1,3}色?)?'
    for name in _all_flower_terms():
        if re.search(r'(纯|只要|仅[要用]|单[\s一]*一?\s*花材?|only)\s*' + _color_gap + re.escape(name), text, re.I) or \
           re.search(re.escape(name) + r'\s*(纯|一束|就好|即可|就行|单色)', text):
            return name
    # 规则 3：花名 + 数量 + 单位（长词优先，先命中「红玫瑰」再考虑「玫瑰」）
    # 数量支持阿拉伯数字与**十位中文数字**（「十一朵粉玫瑰」）；只放行含「十」的写法，
    # 单个「一」不放行——否则「一束玫瑰」会从"普通需求"变成"纯单花束"约束，属行为变更。
    _cn_ten = r'(?:[一二两三四五六七八九]?十[一二三四五六七八九]?)'
    for name in _all_flower_terms():
        if re.search(re.escape(name) + r'\s*(?:\d+|' + _cn_ten + r')\s*[朵支枝束]', text) or \
           re.search(r'(?:\d+|' + _cn_ten + r')\s*[朵支枝束]\s*' + re.escape(name), text) or \
           re.search(r'一\s*[朵支枝束]\s*' + re.escape(name), text):
            # 出现其它不同花材或连接词 → 明确是多花材，整句不判为单花
            other_flower = any(
                n != name and n not in name and name not in n and re.search(re.escape(n), text)
                for n in _all_flower_terms()
            )
            if other_flower or any(w in text for w in _CONNECTIVE_WORDS):
                return None
            return name
    return None

def extract_requirement(text: str) -> FlowerRequirement:
    """共享需求抽取器：自然语言 → 结构化 FlowerRequirement。

    DIY 设计、方案检索、店铺检索共用同一套维度识别，避免散落多处。
    """
    req = FlowerRequirement(raw=text)
    # 用户明确改口时只从新决定提取软维度，避免「不是送女友，是送妈妈」仍被旧词命中。
    corrected = re.search(r'不是[^，。；;]{1,40}?(?:，|,)?\s*(?:而)?是(?=送|给|想|要|为了|表达)', text)
    current = text[corrected.end():] if corrected else text
    req.gift_intent, req.gift_message = understand_gift_message(current)
    for table, key in ((_RECIPIENT_KW, 'recipient'), (_OCCASION_KW, 'occasion'), (_STYLE_KW, 'style'), (_COLOR_KW, 'color'), (_MOOD_KW, 'mood')):
        attr = 'colors' if key == 'color' else key
        if getattr(req, attr):
            continue
        best_kw = None
        for kw in table:
            if kw in current and (best_kw is None or len(kw) > len(best_kw)):
                best_kw = kw
        if best_kw is not None:
            if key == 'color':
                req.colors = [table[best_kw]]
            else:
                setattr(req, key, table[best_kw])
    for kw, sid in _get_scene_map().items():
        if kw in current:
            req.scene = sid
            break
    anchor, exact, bmin, bmax = _extract_budget(text)
    req.budget_anchor = anchor
    req.budget_num = exact
    req.budget_min = bmin
    req.budget_max = bmax
    req.stem_count = _extract_stem_count(text)
    req.single_flower = _detect_single_flower(text)
    if req.recipient:
        req.relationship = _RELATIONSHIP_MAP.get(req.recipient)
    return req

def _extract(text: str) -> dict[str, str]:
    """从自然语言需求中抽取维度（兼容旧形态，供 DIY 设计管线 / _extract_dims 测试）。"""
    return extract_requirement(text).to_legacy_dict()


def merge_requirement(req: FlowerRequirement, llm_req: dict | None) -> FlowerRequirement:
    """把 LLM 的结构化理解**合并进**规则抽取结果 —— **LLM 理解主导，规则兜底**。

    为什么改（2026-09-16，Capri 明确要求「先读懂用户的话，再决定调什么工具，
    而不是识别关键词」）：原实现是 fill-the-gap（**只补空、绝不覆盖**），
    于是**正则先抽到的值会锁死**，模型即使读懂了也改不动——例如正则把口语里
    「就要那种很仙的」漏成默认风格、或把模糊预算判错，模型都无从纠正。
    现在改成：**LLM 读到值就采用 LLM 的**，正则只在模型没读到该字段时兜底。

    护栏一个没少：
      ① prompt 要求「只把你**明确读到**的需求结构化，没说的填 null，不要猜」；
      ② 所有 LLM 值都要过**值域白名单**（规则表值集 / 已知花名表 / 场景表），
         幻觉字段与越界值一律进不来；
      ③ 预算 / 支数仍有数值范围校验；
      ④ **精确字段例外**：支数与单一花材保持「规则优先、只补空」——
         用户明确说出的「11 朵」「纯白百合」由正则按中文数字 / 花名表精确匹配，
         比模型转述可靠，不该被改写。

    Args:
        req: 规则引擎抽出的结构化需求（**兜底基线**）。
        llm_req: 模型输出的 requirements 对象；非 dict（模型没给 / 给了坏值）时原样返回基线。

    Returns:
        合并后的新 FlowerRequirement。
    """
    if not isinstance(llm_req, dict):
        return req
    out = FlowerRequirement(**req.to_dict())

    def _apply_text(attr: str, allowed: set[str]) -> None:
        """LLM 读到合法值 → 采用；没读到（null / 越界）→ 保留规则基线的值。"""
        val = llm_req.get(attr)
        if isinstance(val, str) and val.strip() in allowed:
            setattr(out, attr, val.strip())

    _apply_text('recipient', set(_RECIPIENT_KW.values()))
    _apply_text('occasion', set(_OCCASION_KW.values()))
    _apply_text('style', set(_STYLE_KW.values()))
    _apply_text('mood', set(_MOOD_KW.values()))
    _apply_text('gift_intent', set(INTENT_LANGUAGE))

    raw_colors = llm_req.get('colors')
    candidates = [c.strip() for c in raw_colors if isinstance(c, str)] if isinstance(raw_colors, list) else []
    picked = [c for c in candidates if c in set(_COLOR_KW.values())]
    if picked:
        out.colors = list(dict.fromkeys(picked))[:3]

    val = llm_req.get('scene')
    if isinstance(val, str) and val.strip() in set(_get_scene_map().values()):
        out.scene = val.strip()

    raw_budget = llm_req.get('budget')
    num = _safe_float(raw_budget)
    if num is None and raw_budget is not None:
        # 模型常写成「约200元」「200 左右」「5万」→ 取数字并识别数量级单位
        m = re.search(_AMOUNT_PAT, str(raw_budget))
        if m:
            num = float(m.group(1)) * _MAGNITUDE_UNITS.get(m.group(2) or '', 1.0)
    # 上限 100000（原为 10000）：用户明确说「预算5万」时不该被静默丢弃 —— 丢弃后模型会
    # 退化成默认档位，报价与用户预期完全不符。下限 20 保留（防「1 块钱」这类噪音）。
    if num is not None and 20 <= num <= 100000:
        out.budget_num = num
        out.budget_min = round(num * 0.8)
        out.budget_max = round(num * 1.2)

    raw_stems = llm_req.get('stem_count')
    sc = _safe_int(raw_stems)
    if sc is None and isinstance(raw_stems, str):
        sc = _cn_to_int(raw_stems.strip())   # 模型可能回「十一」这种中文数字
    # ⚠️ 精确字段例外：支数与单一花材是**用户明确说出**的强约束（正则按中文数字 / 花名表
    # 精确匹配，比模型的转述可靠），所以这两个字段保持「只补空」——避免模型把
    # 「就要纯白百合」转述成别的花名、把「11 朵」说成别的数量，反而覆盖用户原话。
    if out.stem_count is None and sc is not None and 1 <= sc <= 999:
        out.stem_count = sc

    val = llm_req.get('single_flower')
    if (not out.single_flower and isinstance(val, str)
            and val.strip() in set(_all_flower_terms())):
        out.single_flower = val.strip()

    if out.recipient and not out.relationship:
        out.relationship = _RELATIONSHIP_MAP.get(out.recipient)
    return out

def _reject_hallucinated_stem_count(rule_req: FlowerRequirement, merged: FlowerRequirement,
                                    text: str) -> FlowerRequirement:
    """丢弃模型凭空补出的支数（**用户原话里对不上**就丢弃）。原地修改并返回 ``merged``。

    为什么必须做（2026-09-18 线上实测）：
    prompt 里原本给模型的示例是「『11 朵』→ 11」，结果**两条完全不同、且都没提支数的**
    请求（预算 300 / 预算 500）都「补召回」出了 ``stem_count=11`` —— 模型把示例值
    当成了默认值。

    支数是**强约束**：`_alloc_stems` 里「用户明确支数」优先级最高、会直接跳过
    「按预算反推」。实测代价：一束 299 元的方案被算成 **101 元**；
    模型随后察觉不对，又自己调一轮 `revise_diy_plan` 去救 ——
    **既报错价、又白烧一整轮 LLM**（单轮 DIY 因此从 ~30s 涨到 60~150s）。

    只校验「模型补出来的」那一档：若规则引擎（含会话累积）已经抽到支数，
    说明用户确实说过，不受影响。

    Args:
        rule_req: 规则引擎抽出的需求（会话累积后）。
        merged: LLM 合并后的需求（可能被写入幻觉支数）。
        text: 本轮用户原话。

    Returns:
        修正后的 ``merged``（幻觉值被置回 None）。
    """
    if rule_req.stem_count is not None or merged.stem_count is None:
        return merged
    if _extract_stem_count(text or '') != merged.stem_count:
        logger.warning('[requirement] 丢弃模型补出的支数 %s —— 用户原话里没有依据（防幻觉）',
                       merged.stem_count)
        merged.stem_count = None
    return merged


def _known_flower(name: str) -> dict:
    """按花名查知识库花卉（含别名匹配），查不到返回空 dict。"""
    if not name:
        return {}
    all_fl = query_knowledge('flower', '')['results']
    for f in all_fl:
        if f['name'] == name or name in f.get('aliases', []):
            return f
    return {}
