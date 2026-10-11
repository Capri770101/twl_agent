"""tools.py —— 内建工具实现。

设计：
- 每个工具用 @register_tool 装饰，自动写入 TOOL_REGISTRY（名称 / 中文描述 / 参数 JSON Schema / 实现）。
- agent 从注册表自动生成「工具说明书」注入 system prompt，并生成 OpenAI function-calling 定义。
- 新增工具只要写一个带装饰器的函数，agent 与提示词零改动。
- 需要用户上下文（如 user_id）的工具加 inject_context=True，execute_tool 时注入 _context。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from typing import Any

from agent.engine.llm import call_llm
from agent.design.quantities import (
    _BUDGET_ORAL as _BUDGET_ORAL, _MAGNITUDE_UNITS as _MAGNITUDE_UNITS,
    _AMOUNT_PAT as _AMOUNT_PAT, _CN_DIGIT as _CN_DIGIT,
    _cn_to_int as _cn_to_int, _extract_budget as _extract_budget,
    _extract_stem_count as _extract_stem_count, _safe_float as _safe_float,
    _safe_int as _safe_int,
)
from agent.design.instructions import (
    _DIY_STEPS_MAX as _DIY_STEPS_MAX, _cap_steps as _cap_steps,
    _build_diy_steps as _build_diy_steps, _build_care_tips as _build_care_tips,
    _build_card_message as _build_card_message,
)
from agent.design.copy_text import (
    _COPY_TEXT_MAX as _COPY_TEXT_MAX, build_plan_copy_text as build_plan_copy_text,
)
from agent.design.stem_allocation import (
    _TIER_MAIN_STEMS as _TIER_MAIN_STEMS, _MAX_MAIN_STEMS as _MAX_MAIN_STEMS,
    _price_table as _price_table, _alloc_stems as _alloc_stems,
)
from agent.design.price_breakdown import (
    _get_tier as _get_tier, _build_budget_breakdown as _build_budget_breakdown,
    _sync_plan_price as _sync_plan_price, _enrich_plan_fees as _enrich_plan_fees,
)
from agent.design.visual_revision import revise_visual_fields
from agent.design.revision_input import _parse_plan, _dims_from_plan, _extract_feedback as _parse_feedback
from agent.engine.ui_protocol import UIType
from agent.knowledge import get_by_id, query_knowledge
from domain.requirements import FlowerRequirement, accumulate
from backend.storage import memory, tasks
from agent.toolkit import register_tool

logger = logging.getLogger('tools')

def _requirement_from_context(_context: dict | None) -> FlowerRequirement | None:
    """从工具上下文取出结构化需求（由 agent 每轮抽取并注入）。"""
    if not _context:
        return None
    req = _context.get('requirement')
    return req if isinstance(req, FlowerRequirement) else None

def _req_clear(req: FlowerRequirement | None) -> bool:
    """需求是否已基本明确（送谁/场合/预算/风格/色系至少一项）。

    只有需求明确后才允许触发「产品推荐 / 店铺推荐」，避免用户在闲聊或
    只问知识时被硬塞卡片。
    """
    return bool(req and (req.recipient or req.occasion or req.budget_num is not None or req.budget_anchor or req.scene or req.style or req.colors))
async def _store_diy_plan(plan: dict, _context: dict | None) -> None:
    """把最新 DIY 方案写入当前会话（会话级，替代旧全局变量，杜绝多用户串号）。

    latest_diy_plan 供生图生成精确 prompt；selected_plan 作为「最近引用方案」，
    让依赖 latest 占位符的后续环节（生图 / 平台下单信息拼装）解析到正确方案。
    """
    uid = (_context or {}).get('user_id', '')
    sid = (_context or {}).get('session_id', '')
    if not uid or not sid:
        return
    await memory.set_session_json(uid, sid, 'latest_diy_plan', plan)
    await memory.set_session_json(uid, sid, 'selected_plan', plan)

async def _resolve_session_plan(plan: str | None, _context: dict | None) -> dict | None:
    """把工具参数里的方案引用解析为具体方案 dict。

    - "latest" / "latest_diy" / 空：会话「最近引用方案」→ 会话最新 DIY 方案；无则返回 None。
    - 显式 plan_id：精确匹配当前会话最新方案或历史快照，未知 ID 不替换为最新方案；
      平台商品（现有方案）已无本地镜像，由 platform_db_query_entity 查询后按需传递。
    - 解析结果与用户、会话绑定，不再依赖进程级全局状态（并发安全）。
    """
    uid = (_context or {}).get('user_id', '')
    sid = (_context or {}).get('session_id', '')
    if plan == 'latest_diy':
        return await memory.get_session_json(uid, sid, 'latest_diy_plan') if sid else None
    if plan in ('latest', '', None):
        if sid:
            selected = await memory.get_session_json(uid, sid, 'selected_plan')
            if selected:
                return selected
            diy = await memory.get_session_json(uid, sid, 'latest_diy_plan')
            if diy:
                return diy
        # 本地商品镜像已移除：会话无方案时返回 None，
        # 平台现有方案由 platform_db_query_entity 实时查询后按需传递。
        return None
    if sid:
        diy = await memory.get_session_json(uid, sid, 'latest_diy_plan')
        if diy and diy.get('plan_id') == plan:
            return diy
        history = await memory.get_session_json(uid, sid, 'diy_plan_versions')
        if isinstance(history, list):
            return next((p for p in history if isinstance(p, dict) and p.get('plan_id') == plan), None)
    return None

async def generate_effect_image(plan: str = 'latest_diy', _context: dict | None = None) -> str:
    """为方案提交 AI 生图任务，返回 task_id / poll / result_url。

    - plan：'latest' / 'latest_diy' / 空 → 解析为会话最近方案（见 _resolve_session_plan）。
    - prompt 优先取方案的 effect_prompt（花材/色彩/形态/包装与方案一致），缺失时回退到描述。
    - 存储：PNG 由 backend/storage.tasks 异步生成并写入 data/generated/，
      result_url 形如 /generated/{task_id}.png（经 /generated 静态挂载访问）。
    - 返回 JSON 字符串，供 agent 的 image_task 渲染器解析。
    """
    plan_obj = await _resolve_session_plan(plan, _context)
    if not plan_obj:
        return json.dumps({'error': '未找到可生图的方案，请先设计或选择方案'}, ensure_ascii=False)
    import hashlib
    visual = plan_obj.get('design') or {}
    identity = {k: visual.get(k) for k in ('color_scheme', 'packaging')}
    for group in ('main_flowers', 'fillers', 'foliage'):
        identity[group] = sorted((str(f.get('name', '')), f.get('qty', 0)) for f in visual.get(group, []) if isinstance(f, dict))
    identity['style'] = plan_obj.get('style')
    identity['shop_id'] = (_context or {}).get('shop_id') or plan_obj.get('shop_id')
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    uid, sid = (_context or {}).get('user_id'), (_context or {}).get('session_id')
    cached = await memory.get_session_json(uid, sid, 'preview_' + digest) if uid and sid else None
    if isinstance(cached, dict) and cached.get('task_id'):
        state = await tasks.get_image_task(cached['task_id'], user_id=uid)
        if state.get('status') in ('processing', 'done'):
            result = {'task_id': cached['task_id'], 'poll': '/tasks/' + cached['task_id'], 'reused': True, 'plan_id': plan_obj.get('plan_id')}
            if state.get('result_url'):
                result['result_url'] = state['result_url']
            return json.dumps(result, ensure_ascii=False)
    prompt = (plan_obj.get('effect_prompt') or plan_obj.get('desc') or plan_obj.get('name') or '花束').strip()
    shop_id = str((_context or {}).get('shop_id') or plan_obj.get('shop_id') or '').strip()
    if shop_id:
        try:
            from agent.shop_style import get_shop_style_profile, style_prompt_suffix
            prompt += style_prompt_suffix(get_shop_style_profile(shop_id))
        except Exception:
            logger.warning('[tools] 店铺风格参考提取失败，回退原始生图 prompt', exc_info=True)
    if not prompt:
        return json.dumps({'error': '方案缺少可生图的描述信息'}, ensure_ascii=False)
    task_id = (await tasks.get_or_create_preview(prompt, uid, sid, digest) if uid and sid
               else await tasks.create_image_task(prompt, user_id=uid))
    result: dict[str, Any] = {'task_id': task_id, 'poll': f'/tasks/{task_id}', 'plan_id': plan_obj.get('plan_id')}
    try:
        st = await tasks.get_image_task(task_id, user_id=(_context or {}).get('user_id'))
        if st.get('status') == 'done' and st.get('result_url'):
            result['result_url'] = st['result_url']
    except Exception:
        logger.debug('[tools] 生图任务即时查询失败 task_id=%s', task_id)
    return json.dumps(result, ensure_ascii=False)

@register_tool(name='retrieve_knowledge', description='检索花卉 DIY 知识库：花材(花语/季节/价格档/搭配性)、风格、搭配规则、预算、包装、颜色色值和鲜切花养护。设计花艺方案前或询问养护时调用；商家信息使用已授权的平台店铺或客服工具，不属于本地知识域。', parameters={'type': 'object', 'properties': {'domain': {'type': 'string', 'description': '检索域：flower(花材) | style(风格) | pairing(搭配) | budget(预算) | packaging(包装) | care(养护) | colors(颜色) | scene(场景) | proven(已验证方案) | all(全部)'}, 'query': {'type': 'string', 'description': '关键词或自然语言，如 母亲/生日/北欧/200元/怎么养得久/香槟色'}}, 'required': ['domain', 'query']}, tags=['knowledge'])
def retrieve_knowledge(domain: str, query: str) -> str:
    """检索知识库，返回相关条目 JSON。"""
    return json.dumps(query_knowledge(domain, query), ensure_ascii=False)

_RECIPIENT_KW = {'妈妈': '母亲', '母亲': '母亲', '妈': '母亲', '娘': '母亲', '恋人': '恋人', '女朋友': '恋人', '男朋友': '恋人', '老婆': '恋人', '老公': '恋人', '对象': '恋人', '爱人': '恋人', '男友': '恋人', '女友': '恋人', '先生': '恋人', '丈夫': '恋人', '朋友': '朋友', '闺蜜': '朋友', '兄弟': '朋友', '同事': '朋友', '姐妹': '朋友', '自己': '自己', '悦己': '自己', '我': '自己', '长辈': '长辈', '老人': '长辈', '父母': '长辈', '领导': '长辈', '上司': '长辈', '老板': '长辈', '老师': '长辈', '宝宝': '宝宝', '婴儿': '宝宝', '新生儿': '宝宝'}
_OCCASION_KW = {'生日': '生日', '庆祝': '生日', '母亲节': '母亲', '父亲节': '父亲', '节': '节日', '告白': '告白', '表白': '告白', '纪念日': '告白', '求婚': '告白', '婚礼': '婚礼', '结婚': '婚礼', '领证': '婚礼', '探病': '探病', '生病': '探病', '康复': '探病', '住院': '探病', '道歉': '道歉', '对不起': '道歉', '抱歉': '道歉', '毕业': '毕业', '乔迁': '乔迁', '开业': '开业', '升职': '升职', '入职': '入职'}
_STYLE_KW = {'韩式': 'S_KOREAN', '韩系': 'S_KOREAN', '北欧': 'S_NORDIC', '简约': 'S_NORDIC', '极简': 'S_NORDIC', '复古': 'S_VINTAGE', '古典': 'S_VINTAGE', '港风': 'S_VINTAGE', '中古': 'S_VINTAGE', '自然': 'S_NATURAL', '野趣': 'S_NATURAL', '田园': 'S_NATURAL', 'ins': 'S_INS', 'ins风': 'S_INS', '网红': 'S_INS', '奶油风': 'S_INS', '法式': 'S_INS', '日式': 'S_JAPANESE', '禅': 'S_JAPANESE', '日系': 'S_JAPANESE'}
_COLOR_KW = {'红': '红', '粉': '粉', '白': '白', '香槟': '香槟', '紫': '紫', '蓝': '蓝', '黄': '黄', '橙': '橙', '绿': '绿', '多彩': '多彩混合', '亮': '亮', '鲜艳': '亮', '缤纷': '多彩混合', '粉嫩': '粉', '浅粉': '粉', '桃红': '粉', '正红': '红', '酒红': '红', '橘': '橙', '鹅黄': '黄', '天蓝': '蓝', '湖蓝': '蓝', '香槟色': '香槟', '五彩': '多彩混合', '撞色': '多彩混合'}
_MOOD_KW = {'温柔': '温柔', '温馨': '温馨', '浪漫': '浪漫', '清新': '清新', '热烈': '热烈', '活泼': '活泼', '高级': '高级', '素雅': '素雅', '优雅': '优雅', '莫兰迪': '素雅', '马卡龙': '清新', '小清新': '清新', '轻奢': '高级', '低调': '素雅', '治愈': '治愈', '安静': '素雅', '甜美': '甜美', '氛围感': '优雅', '高级感': '高级'}

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

def _extract_feedback(feedback: str) -> dict[str, Any]:
    """解析自然语言反馈为调整指令：维度覆盖 + 需移除花材集合。"""
    return _parse_feedback(feedback, style_keywords=_STYLE_KW,
                           color_keywords=_COLOR_KW, mood_keywords=_MOOD_KW,
                           flower_names=_get_all_flower_names)
_RELATIONSHIP_MAP = {'母亲': '亲子', '恋人': '情侣', '朋友': '朋友', '自己': '自用', '长辈': '长辈/同事', '宝宝': '亲子'}

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
    for table, key in ((_RECIPIENT_KW, 'recipient'), (_OCCASION_KW, 'occasion'), (_STYLE_KW, 'style'), (_COLOR_KW, 'color'), (_MOOD_KW, 'mood')):
        attr = 'colors' if key == 'color' else key
        if getattr(req, attr):
            continue
        best_kw = None
        for kw in table:
            if kw in text and (best_kw is None or len(kw) > len(best_kw)):
                best_kw = kw
        if best_kw is not None:
            if key == 'color':
                req.colors = [table[best_kw]]
            else:
                setattr(req, key, table[best_kw])
    for kw, sid in _get_scene_map().items():
        if kw in text:
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


def _fallback_flower(all_flowers: dict, category: str, preferred: str,
                     exclude: set[str]) -> list[dict]:
    """填充 / 叶材的同类兜底：优先 ``preferred``，被排除时改取同类未排除的第一个。

    Args:
        all_flowers: 知识库花材表（name → 花材 dict，含 ``category``）。
        category: 目标类别（``填充`` / ``叶材``）。
        preferred: 首选花名（原来的硬编码兜底项）。
        exclude: 用户明确不要的花材集合。

    Returns:
        单元素列表或空列表（一个可用候选都没有时**留空**）。

    ⚠️ 为什么不直接 ``or [all_flowers.get(preferred)]``（2026-09-20 实测缺陷）：
    用户说「不要满天星」后，只要候选里没有别的填充花材，旧写法就把满天星**塞回来** ——
    卡片里残留「满天星×5」，用户看到的是「说了不要还给我」。
    这里确立优先级：**「绝不把用户明确不要的花材加回来」>「方案必须配齐配材/叶材」**。
    """
    first = all_flowers.get(preferred)
    if isinstance(first, dict) and preferred not in exclude:
        return [first]
    for name, flower in all_flowers.items():
        if name in exclude or not isinstance(flower, dict):
            continue
        if flower.get('category') == category:
            return [flower]
    return []


def _resolve_flowers(dims: dict[str, str], style: dict, budget_tier: dict, prefer_flowers: list[str] | None=None, exclude_flowers: set[str] | None=None, single_flower: str | None=None) -> tuple[list[dict], list[dict], list[dict]]:
    """根据维度 + 风格 + 预算，从知识库挑主花/配材/叶材。

    Args:
        prefer_flowers: 场景模板指定的优先主花（最高权重）。
        exclude_flowers: 用户反馈中要求移除的花材名集合（迭代时用到）。
    """
    prefer_flowers = prefer_flowers or []
    exclude_flowers = exclude_flowers or set()
    # 单一花材意图：严格只返回该花，不强行补配材/叶材（尊重「纯X」「只要X」）。
    if single_flower:
        f = _known_flower(single_flower)
        if f:
            return ([f], [], [])
    rec_recipient: list[str] = []
    rec_occasion: list[str] = []
    all_fl = query_knowledge('flower', '')['results']
    if dims.get('recipient'):
        for p in query_knowledge('pairing', dims['recipient'])['results']:
            for f in all_fl:
                if f['name'] in p.get('recommendation', ''):
                    rec_recipient.append(f['name'])
    if dims.get('occasion'):
        for p in query_knowledge('pairing', dims['occasion'])['results']:
            for f in all_fl:
                if f['name'] in p.get('recommendation', ''):
                    rec_occasion.append(f['name'])
    color = dims.get('color')
    style_flowers = style.get('typical_flowers', [])
    budget_flowers = budget_tier.get('suggested_flowers', [])
    all_flowers = {f['name']: f for f in query_knowledge('flower', '')['results']}
    candidates = []
    seen = set()
    for name in list(prefer_flowers) + list(rec_recipient) + list(rec_occasion) + list(style_flowers) + list(budget_flowers):
        f = all_flowers.get(name)
        if not f or name in seen or name in exclude_flowers:
            continue
        seen.add(name)
        candidates.append(f)
    if color and color != '亮' and (color != '多彩混合'):
        colored = [f for f in candidates if color in f.get('colors', [])]
        if colored:
            candidates = colored
    if not candidates:
        candidates = [all_flowers[n] for n in style_flowers if n in all_flowers and n not in exclude_flowers]
    main_candidates = [f for f in candidates if f.get('category') == '主花'] or candidates
    style_set = set(style.get('typical_flowers', []))
    rec_rec_set = set(rec_recipient)
    rec_occ_set = set(rec_occasion)
    prefer_set = set(prefer_flowers)
    scored = []
    for idx, f in enumerate(main_candidates):
        if f['name'] in prefer_set:
            w = -1
        elif f['name'] in rec_rec_set:
            w = 0
        elif f['name'] in style_set:
            w = 1
        elif f['name'] in rec_occ_set:
            w = 2
        else:
            w = 3
        scored.append((w, idx, f))
    scored.sort(key=lambda x: (x[0], x[1]))
    main = [f for _, _, f in scored][:2] or candidates[:1]
    fillers = [f for f in candidates if f.get('category') == '填充' and f['name'] not in exclude_flowers][:1]
    if not fillers:
        # ⚠️ 兜底也必须尊重排除：旧写法是无条件 `or [满天星]`，会把用户明确
        # 不要的满天星塞回来（2026-09-20 实测「不要满天星」后卡片仍残留满天星×5）。
        fillers = _fallback_flower(all_flowers, '填充', '满天星', exclude_flowers)
    foliage = [f for f in candidates if f.get('category') == '叶材' and f['name'] not in exclude_flowers][:1]
    if not foliage:
        foliage = _fallback_flower(all_flowers, '叶材', '尤加利', exclude_flowers)
    return ([f for f in main if f], [f for f in fillers if f], [f for f in foliage if f])
def _known_flower(name: str) -> dict:
    """按花名查知识库花卉（含别名匹配），查不到返回空 dict。"""
    if not name:
        return {}
    all_fl = query_knowledge('flower', '')['results']
    for f in all_fl:
        if f['name'] == name or name in f.get('aliases', []):
            return f
    return {}

def _suitable_for(recipient: str, occasion: str) -> list[str]:
    """规则兜底：由收礼人/场合推导适宜人群标签（模块二卡片字段）。"""
    tags: list[str] = []
    r = recipient or ''
    if any(k in r for k in ('恋人', '老公', '老婆', '女友', '男友')):
        tags += ['恋人', '表白']
    elif any(k in r for k in ('妈妈', '母亲', '奶奶', '外婆', '爸爸', '父亲', '长辈')):
        tags += ['长辈', '感恩']
    elif any(k in r for k in ('同事', '领导', '客户')):
        tags += ['同事', '职场']
    elif any(k in r for k in ('朋友', '闺蜜', '兄弟', '同学')):
        tags += ['朋友']
    if not tags:
        tags = ['通用']
    if occasion == '生日':
        tags.append('生日')
    return list(dict.fromkeys(tags))

def _build_caution(main: list[dict]) -> str:
    """规则兜底：禁忌/提醒文案（模块二卡片字段）。"""
    out: list[str] = []
    names = [f.get('name') or '' for f in main]
    if any('百合' in n or '郁金香' in n for n in names):
        out.append('百合/郁金香花粉较易致敏，过敏体质请谨慎接触')
    if any('满天星' in n or '小雏菊' in n for n in names):
        out.append('花材较为娇嫩，拆包装时请轻拿轻放')
    out.append('鲜花忌暴晒与空调直吹，收到后斜剪花枝根部并每日换水，花期更持久')
    return '；'.join(out)

def _mood_tags(color_scheme: list[str], tone: str) -> list[str]:
    """规则兜底：由色板与场景基调推导情绪标签（模块二卡片字段，文字版）。"""
    tags: list[str] = []
    for c in color_scheme or []:
        if any(w in c for w in ('红', '橙', '玫')) and '热烈' not in tags:
            tags.append('热烈')
        if any(w in c for w in ('蓝', '绿', '青', '白')) and '宁静' not in tags:
            tags.append('宁静')
        if any(w in c for w in ('粉', '香槟', '奶', '米')) and '温柔' not in tags:
            tags.append('温柔')
    if not tags:
        tags = ['温柔']
    if tone and tone not in tags:
        tags.append(tone)
    return tags[:3]

def _build_plan(dims: dict[str, str], version: int=1, parent_id: str | None=None, exclude_flowers: set[str] | None=None, shop_id: str = '') -> dict:
    """设计核心：基于维度组装一份结构化 DIY 方案（场景感知 + 细分风格）。

    Args:
        dims: 抽取出的维度（recipient/occasion/style/substyle/scene/color/mood/budget/_keep_main）。
        version: 方案版本号，迭代时递增。
        parent_id: 上一版方案 id，便于追溯。
        exclude_flowers: 反馈中要求移除的花材名集合。
        shop_id: 店铺 ID —— 决定用哪套花材价表（该店实测 → 全网基线，见 `agent/pricing`）。
    """
    scene = get_by_id('scene', dims.get('scene')) if dims.get('scene') else None
    style_id = dims.get('style') or (scene.get('recommended_style') if scene else None) or 'S_KOREAN'
    substyle_id = dims.get('substyle')
    if not substyle_id and scene:
        substyle_id = scene.get('recommended_substyle')
    if not substyle_id:
        substyle_id = _infer_substyle(style_id, dims)
    resolved, parent = (None, None)
    if substyle_id:
        resolved, parent = _get_style_full(substyle_id)
    if not resolved:
        resolved, parent = _get_style_full(style_id)
    style = resolved or get_by_id('style', 'S_KOREAN')
    parent_style = parent or style
    style_label = style.get('name', parent_style.get('name', '韩式'))
    budget_num = int(dims['budget']) if dims.get('budget') else None
    tier = _get_tier(budget_num, scene.get('budget_anchor') if scene else None)
    single_flower = dims.get('single_flower')
    stem_count = int(dims['stem_count']) if dims.get('stem_count') not in (None, '', 'null') else None
    prefer = list(scene.get('main_flower_preference', [])) if scene else []
    keep_main = [n for n in dims.get('_keep_main', '').split(',') if n] if dims.get('_keep_main') else []
    main, fillers, foliage = _resolve_flowers(dims, style, tier, prefer_flowers=prefer or keep_main, exclude_flowers=exclude_flowers, single_flower=single_flower)
    main_flowers = [{'name': f['name'], 'role': '主花', 'flower_language': f.get('flower_language', [])} for f in main]
    filler_flowers = [{'name': f['name'], 'role': '填充'} for f in fillers]
    foliage_flowers = [{'name': f['name'], 'role': '叶材'} for f in foliage]
    table = _price_table(shop_id)
    stems = _alloc_stems(tier, main, fillers, foliage, budget_num, stem_count, table)
    for fl in main_flowers + filler_flowers + foliage_flowers:
        fl['qty'] = stems.get(fl['name'], 1)
        fl['unit_price'] = round(table.unit_price(fl['name']), 1)
    packaging = get_by_id('packaging', 'PK_BOUQUET')
    important = dims.get('occasion') in ('告白', '生日') or (scene and scene['id'] in ('SC_WEDDING', 'SC_ANNIVERSARY', 'SC_NEWYEAR'))
    if '高档' in tier['label'] or important:
        packaging = get_by_id('packaging', 'PK_BOX') or packaging
    color_scheme = list(style.get('color_palette', [])) or list(parent_style.get('color_palette', []))
    if scene:
        tone = [c for c in scene.get('color_tone', []) if c not in color_scheme]
        color_scheme = tone + color_scheme
    if dims.get('color') and dims['color'] not in ('亮', '多彩混合') and (dims['color'] not in color_scheme):
        color_scheme = [dims['color']] + color_scheme
    meanings = []
    for f in main:
        meanings.extend(f.get('flower_language', []))
    meaning = '、'.join(dict.fromkeys(meanings)) or '美好心意'
    if scene:
        meaning = f"{meaning}（{scene.get('meaning_tone', '')}）"
    short_meaning = '、'.join(list(dict.fromkeys(meanings))[:2]) or '美好心意'
    tone = scene.get('meaning_tone', '') if scene else ''
    lo, hi = tier['range']
    est = f'{lo}-{hi} 元' if budget_num is None else f"约 {budget_num} 元（{tier['label']}档）"
    # 生图提示词：必须带**支数**（用户 2026-09-16 要求「效果图的花的数量也要保持一致」）。
    # 统一走 _effect_prompt_from_design；单一花材的纯花描述也在其中处理，
    # 避免此处再写一遍导致两处口径不一致。
    effect_prompt = _effect_prompt_from_design(
        {'main_flowers': main_flowers, 'fillers': filler_flowers,
         'foliage': foliage_flowers, 'color_scheme': color_scheme},
        style_label, packaging['name'] if packaging else '花束')
    occ_label = dims.get('occasion') or (scene['name'] if scene else '定制')
    notes = []
    if scene:
        notes.append(f"场景模板：{scene['name']} —— {scene.get('notes', '')}")
    notes.append(f"风格：{style_label}（{style.get('description', '')}）")
    # 单一花材方案不展示预算档模板的配材举例（如「满天星/小雏菊」），避免误导为方案内容。
    if single_flower:
        notes.append(f"预算档：{tier['label']}")
    else:
        notes.append(f"预算档：{tier['label']}（{tier['config']}）")
    if exclude_flowers:
        notes.append(f"已按反馈移除：{'、'.join(sorted(exclude_flowers))}")
    freshness_days = {'高': '约 7-10 天', '中': '约 5-7 天', '低': '约 3-5 天'}
    fvals = [f.get('freshness') for f in main if f.get('freshness')]
    worst = min(fvals, key=lambda v: {'高': 0, '中': 1, '低': 2}.get(v, 1)) if fvals else '中'
    shelf_life = freshness_days.get(worst, '约 5-7 天')
    hard = important or '高档' in tier['label']
    difficulty = '高手' if hard and len(main) >= 3 else '进阶' if hard else '入门'
    est_time = 45 if hard else 30
    suitable_for = _suitable_for(dims.get('recipient', ''), occ_label)
    caution = _build_caution(main)
    mood_tags = _mood_tags(color_scheme, tone)
    _stems = _alloc_stems(tier, main, fillers, foliage, budget_num, stem_count, table)
    _flower_qty_text = '、'.join(f"{f['name']}×{_stems.get(f['name'], 1)}" for f in main + fillers + foliage if f) or '玫瑰×10'
    # 两段式定价的「基础费」：包装 + 手工 + 基础服务，来自价表实测（不再是拍脑袋的档位常数）
    base_fee = float(table.base_fee)
    _source_label = {'override': '商家设定的标准价', 'merchant': '本店在售商品',
                     'baseline': '平台在售商品'}.get(table.source, '平台在售商品')
    plan = {'plan_id': 'DIY_' + uuid.uuid4().hex[:6], 'version': version, 'parent_id': parent_id, 'name': f'{style_label}·{occ_label}花束', 'diy': True, 'style': style_label, 'style_id': style_id, 'substyle_id': substyle_id, 'substyle': style.get('name') if substyle_id and resolved is not parent else None, 'recipient': dims.get('recipient', '通用'), 'occasion': occ_label, 'scene_id': scene['id'] if scene else None, 'scene': scene['name'] if scene else None, 'budget_num': budget_num, 'budget_tier': tier['label'], 'design': {'main_flowers': main_flowers, 'fillers': filler_flowers, 'foliage': foliage_flowers, 'color_scheme': color_scheme, 'packaging': packaging['name'] if packaging else '花束', 'meaning': meaning, 'notes': notes, 'difficulty': difficulty, 'est_time': est_time, 'shelf_life': shelf_life, 'suitable_for': suitable_for, 'caution': caution, 'mood_tags': mood_tags, 'fees': {'base_fee': base_fee, 'base_standard': f'包装、手工与基础服务 {base_fee:.0f} 元/束（按{_source_label}推算）', 'stem_count': _flower_qty_text, 'note': f'单支花材价按{_source_label}推算，已含包装与手工；下单前以门店确认为准。', 'price_source': table.source}}, 'estimated_price': est, 'effect_prompt': effect_prompt, 'desc': f"为你设计了一份{style_label}{occ_label}花束：花材共 {_flower_qty_text}，色调{'/'.join(color_scheme)}，寓意{meaning}。含包装与手工 {base_fee:.0f} 元，预算{est}。", 'diy_steps': _build_diy_steps(main, fillers, foliage, color_scheme, packaging), 'care_tips': _build_care_tips(main), 'card_message': _build_card_message(dims.get('recipient', '朋友'), scene['name'] if scene else occ_label, style_label, tone, short_meaning), 'budget_breakdown': _build_budget_breakdown(main, fillers, foliage, packaging, tier, budget_num, stem_count, table)}
    if exclude_flowers:
        # 持久化「用户明确不要的花材」：改版时由 revise_with_llm 并集继承。
        # 不记的话，下一轮用户只说「换个配色」，被排除的花材就会**悄悄回来**
        # （2026-09-20 实测：说了「不要满天星」的下一版仍出现满天星）。
        plan['exclude_flowers'] = sorted(exclude_flowers)
    # 顶层数值价格（2026-09-18，外部审计 P0-3 修复）。
    # 问题：原实现只有 `estimated_price` 字符串（"约 300 元（轻送礼档）"）与嵌套的
    # `budget_breakdown.total_estimate`，**没有顶层数值字段**。接入方按平台商品卡惯例取
    # `plan['price']` 得到 undefined，前端 `Math.round((e.price||0)*100)` 算出 0
    # → 一束约 300 元的花束以 **0 元**进购物车并跳结算页（下游真实事故）。
    # 现在统一走 _sync_plan_price：数值 + 展示文案 + 单位一次写齐，
    # 且**任何重算 budget_breakdown 的地方都要回到它收口**（否则两者会分叉）。
    _sync_plan_price(plan, tier['label'])
    # 打标：供 _merge_plan 强制单一花材 / 明确支数（LLM 输出不得违背用户显式要求）。
    if single_flower:
        plan['_single_flower'] = single_flower
    if stem_count is not None:
        plan['stem_count'] = stem_count
    return plan

def _retrieve_for_design(requirements: str) -> str:
    """把知识库 RAG 检索结果格式化为 LLM 可读的上下文（候选花材/风格/场景/实战方案）。"""
    parts: list[str] = []
    proven = query_knowledge('proven', requirements)['results'][:3]
    if proven:
        lines = []
        for s in proven:
            flowers = '、'.join(s.get('flowers') or [])
            lines.append(f"- {s.get('name')}（风格:{s.get('style') or '-'}，对象:{s.get('recipient') or '-'}，场合:{s.get('occasion') or '-'}，预算:{s.get('budget') or '-'}元，主花:{flowers}，寓意:{s.get('meaning') or '-'}" + (f"，已成交 {s.get('order_count')} 次" if s.get('order_count') else '') + '）')
        parts.append('【历史实战方案（用户验证过，可参考其组合但请结合本次需求微调）】\n' + '\n'.join(lines))
    flowers = query_knowledge('flower', requirements)['results'][:8]
    if flowers:
        lines = [f"- {f['name']}（花语：{'、'.join(f.get('flower_language', []))}；可选色：{'、'.join(f.get('colors', []))}；搭配：{f.get('pairing_notes', '')}）" for f in flowers]
        parts.append('【候选花材】\n' + '\n'.join(lines))
    styles = query_knowledge('style', requirements)['results'][:5]
    if styles:
        lines = [f"- {s['name']}：{s.get('description', '')}（调色板：{','.join(s.get('color_palette', []))}）" for s in styles]
        parts.append('【候选风格】\n' + '\n'.join(lines))
    scenes = query_knowledge('scene', requirements)['results'][:4]
    if scenes:
        lines = [f"- {s['name']}：{s.get('notes', '')}（推荐主花：{','.join(s.get('main_flower_preference', []))}）" for s in scenes]
        parts.append('【候选场景】\n' + '\n'.join(lines))
    return '\n\n'.join(parts) if parts else '（知识库暂无相关召回）'

def _anchor_style(plan: dict) -> None:
    """就地把 plan['style'] 名反查锚定到一致的 style_id / substyle（修法 B）。

    背景：baseline 用规则推导 style_id，LLM 又自由生成 style 名（如『自然风』），二者常错位
    （style='自然风' 但 style_id='S_KOREAN'）。这里以最终 style 名为语义意图，反查知识库锚定
    一致的 style_id（及 substyle）；找不到则保留 baseline 的 style_id，保证不引入错误映射。
    若匹配到顶层风格但 baseline 带着属于旧风格的 substyle，则清掉以免错配。
    """
    final_style = plan.get('style')
    if not final_style:
        return
    sid, sub_id, sub_name = _match_style(final_style)
    if not sid:
        return
    plan['style_id'] = sid
    if sub_id:
        plan['substyle_id'] = sub_id
        plan['substyle'] = sub_name
    elif plan.get('substyle_id'):
        plan['substyle_id'] = None
        plan['substyle'] = None

def _modern_packaging_prompt(packaging: str, style_label: str = '') -> str:
    """把抽象包装名转成生图模型可理解的现代包装视觉规范。"""
    text = f'{packaging} {style_label}'
    if any(k in text for k in ('礼盒', '盒')):
        return '现代结构礼盒包装：硬挺盒体、内衬固定花泥、盒口有层次外翻纸艺、细缎带或烫金小卡点缀，像当代花店精品礼盒，不是老式纸盒'
    if '瓶插' in text or '花瓶' in text or '玻璃' in text or '亚克力' in text:
        return '现代桌面瓶插：简洁透明玻璃或磨砂器皿，花材松弛有留白，器皿比例精致，适合当代家居摄影，不是老式塑料花瓶'
    if any(k in text for k in ('花篮', '提篮', '篮')):
        return '现代手提花篮包装：轻量藤编或极简哑光提篮，花材自然高低错落，搭配织带提手和少量装饰，不是传统对称大花篮'
    if any(k in text for k in ('牛皮', '自然', '北欧')):
        return '现代自然系包装：双层或多层牛皮纸与半透明硫酸纸叠搭，露出枝干和叶材线条，麻绳或窄织带收口，松弛但有设计感'
    if any(k in text for k in ('雾面', '韩素', '韩式', 'ins', '奶油')):
        return '现代花店包装：两到三层低饱和雾面纸叠搭，内层半透明纸形成褶皱和层次，外层不规则折角，细缎带或窄丝带收口，留白充足，精致轻盈'
    if any(k in text for k in ('单支', '简包')):
        return '现代单支花包装：小面积双层半透明纸与窄丝带，保留花茎线条和呼吸感，简洁但有精致细节，不是随意一层纸包裹'
    return '现代花店精品包装：多层纸材叠搭、立体折角、适量半透明材质、细缎带收口和精致小卡，包装有层次和设计感，避免单层纸和老式大蝴蝶结'


def _effect_prompt_from_design(design: dict, style_label: str = '韩式',
                               packaging: str = '花束') -> str:
    """按方案的**真实花材与支数**重建生图提示词（确定性生成，不允许 LLM 自由发挥）。

    Args:
        design: 方案 design 段（含 main_flowers / fillers / foliage / color_scheme）。
        style_label: 风格中文名。
        packaging: 包装中文名。

    Returns:
        供 qwen-image 使用的提示词字符串。

    为什么（2026-09-16 用户反馈）：原实现只把**花名**写进提示词、不带支数，生图模型
    自行发挥数量 → 效果图的花量与方案清单对不上（用户要求「效果图的花的数量也要保持
    一致」）。这里把「花名 + 支数」+ 总数写死进提示词；且必须在 `_merge_plan` 的
    **支数校正之后**调用，保证与最终清单完全一致。
    """
    def _fmt(items: Any) -> str:
        parts: list[str] = []
        for f in items or []:
            if not isinstance(f, dict):
                if f:
                    parts.append(str(f))
                continue
            name = str(f.get('name') or '').strip()
            if not name:
                continue
            qty = f.get('qty')
            if isinstance(qty, (int, float)) and int(qty) > 0:
                parts.append(f'{name} {int(qty)} 枝')
            else:
                parts.append(name)
        return '、'.join(parts)

    design = design or {}
    main_raw = design.get('main_flowers') or []
    main = _fmt(main_raw)
    fillers = _fmt(design.get('fillers'))
    foliage = _fmt(design.get('foliage'))
    colors = '/'.join(design.get('color_scheme') or []) or '温柔粉'
    pk = packaging or design.get('packaging') or '花束'
    total = 0
    for grp in ('main_flowers', 'fillers', 'foliage'):
        for f in design.get(grp) or []:
            if isinstance(f, dict):
                q = f.get('qty')
                if isinstance(q, (int, float)) and int(q) > 0:
                    total += int(q)

    # 单一花材：只描述一种花，避免残留「搭配满天星」等外搭措辞。
    packaging_visual = _modern_packaging_prompt(pk, style_label)
    modern_negative = '避免老旧十年前花店风、单层牛皮纸、廉价塑料包装、过大俗气蝴蝶结、对称传统大花束、过度拥挤、塑料质感'
    if main and len(main_raw) == 1 and not fillers and not foliage:
        f0 = main_raw[0] if isinstance(main_raw[0], dict) else {}
        nm = str(f0.get('name') or main).strip()
        q = f0.get('qty')
        qtext = f'共 {int(q)} 枝' if isinstance(q, (int, float)) and int(q) > 0 else ''
        return (f'{style_label}风格纯{nm}花束，仅使用{nm}一种花材'
                f'{("（" + qtext + "）") if qtext else ""}。方案包装类型为「{pk}」；{packaging_visual}，色调统一，'
                f'背景干净柔和，现代花店电商摄影，摄影级静物，高级感。{modern_negative}')

    segs: list[str] = []
    if main:
        segs.append(f'主花 {main}')
    if fillers:
        segs.append(f'配花 {fillers}')
    if foliage:
        segs.append(f'叶材 {foliage}')
    qty_note = f'整束共 {total} 枝' if total else ''
    detail = '；'.join(segs) or '花材随机搭配'
    return (f'{style_label}风格花束，严格按下列花材与枝数插制'
            f'（数量不要增减、花材不要替换）：{detail}。{qty_note}，'
            f'色调{colors}。方案包装类型为「{pk}」；{packaging_visual}，背景干净柔和，现代花店电商摄影，摄影级静物，高级感。{modern_negative}')


def _design_qty_map(design: dict) -> dict[str, int]:
    """方案里各花材的**真实支数**（authoritative），用于校正文字描述里的数量。"""
    out: dict[str, int] = {}
    for grp in ('main_flowers', 'fillers', 'foliage'):
        for f in design.get(grp) or []:
            if not isinstance(f, dict):
                continue
            name = str(f.get('name') or '').strip()
            qty = f.get('qty')
            if name and isinstance(qty, (int, float)) and int(qty) > 0:
                out[name] = int(qty)
    return out


def _sync_flower_qty(text: str, qty: dict[str, int]) -> str:
    """把文字里「花名×N」/「N朵花名」的数量改写成方案的**真实支数**。

    为什么需要：``desc`` / ``diy_steps`` 由 LLM 按**它自己**的支数写，而最终支数由
    预算分配（``_alloc_stems``）决定。两者不一致时用户会看到自相矛盾的方案——
    线上实测（2026-09-16）：描述写「粉色康乃馨×12 配洋桔梗×4 及尤加利叶×3」，
    而花材清单是「康乃馨×3 枝、洋桔梗×2 枝」。

    只改**已知花名**紧邻的数量，其他数字（预算金额、天数、温度、cm）一律不动。
    """
    if not text or not qty:
        return text
    names = sorted(qty, key=len, reverse=True)          # 长名优先，避免「玫瑰」吃掉「粉玫瑰」
    alt = '|'.join(re.escape(n) for n in names)
    text = re.sub(
        r'(' + alt + r')\s*[×xX＊*]\s*(\d{1,3})',
        lambda m: f'{m.group(1)}×{qty[m.group(1)]}',
        text,
    )
    text = re.sub(
        r'(\d{1,3})\s*[朵支枝]\s*([\u4e00-\u9fff]{0,2}?)(' + alt + r')',
        lambda m: f'{m.group(2)}{m.group(3)}×{qty[m.group(3)]}',
        text,
    )
    return text


def _apply_flower_exclusions(plan: dict, exclude: set[str], baseline_design: dict | None = None) -> list[str]:
    """把「用户明确不要的花材」从**最终方案**里确定性剔除（模型不给面子时也生效）。

    Args:
        plan: 已合并的方案（原地修改）。
        exclude: 用户不要的花名集合。
        baseline_design: 规则引擎基线里的 ``design``（主花被清空时用于回落）。

    Returns:
        实际剔除的花名（去重前的发现顺序），供日志与测试断言。

    为什么必须在**合并之后**再剔一遍（2026-09-20 实测缺陷）：
    ``_build_plan(exclude_flowers=…)`` 只保证 **baseline** 干净，而 ``_merge_plan`` 是
    「LLM 有则覆盖」—— 模型照样能把被排除的花材写回来。实测用户说「不要满天星」，
    新版卡片配材里仍是「满天星×5」，模型自己都在文案里承认「残留了一条」。

    ⚠️ 必须早于 ``real_main/fillers/foliage`` 与 ``_enrich_plan_fees``：后者按最终花材
    重算 diy_steps / 明细 / 顶层价格。晚一步就会变成「用料去掉了、报价还是旧的」。
    """
    if not exclude:
        return []
    design = plan.get('design')
    if not isinstance(design, dict):
        return []
    removed: list[str] = []
    for group in ('main_flowers', 'fillers', 'foliage'):
        items = design.get(group)
        if not isinstance(items, list):
            continue
        kept: list[Any] = []
        for flower in items:
            name = str(flower.get('name') or '').strip() if isinstance(flower, dict) else ''
            if name and name in exclude:
                removed.append(name)
            else:
                kept.append(flower)
        design[group] = kept
    if not design.get('main_flowers'):
        # 主花被清空（用户说「不要玫瑰」而候选里没别的）→ 不能留一个没有主花的方案。
        # 从 baseline 取（它本就按排除集构建过）；仍为空就交给上游的兜底出卡处理。
        fallback = [m for m in ((baseline_design or {}).get('main_flowers') or [])
                    if isinstance(m, dict) and str(m.get('name') or '') not in exclude]
        if fallback:
            design['main_flowers'] = fallback
    if removed:
        # notes 里若提到被排除的花材，一并去掉（否则卡片说明里还留着它的名字）
        terms = [t for t in _all_flower_terms() if t in exclude]
        notes = design.get('notes')
        if isinstance(notes, list) and terms:
            design['notes'] = [n for n in notes
                               if not (isinstance(n, str) and any(t in n for t in terms))]
        logger.info('[设计] 已强制移除用户排除的花材：%s', '、'.join(dict.fromkeys(removed)))
    return removed


_NEGATION_MARKERS = ('去除', '去掉', '不要', '不含', '没有', '无需', '剔除', '换掉', '省略', '不加', '免去', '无')


def _mentions_excluded(text: str, names: list[str]) -> bool:
    """文本是否**正面**提到了被排除的花材（否定表述不算）。

    ⚠️ 为什么不能「见名就删」：模型常写「**去除了**满天星的繁复感」——这在
    「用户不要满天星」的语境下是**正确且加分**的表述，误删反而把对的改错。
    只有「配满天星」「点缀满天星」这类正面提及才需要处理。
    """
    for name in names:
        if not name:
            continue
        start = 0
        while True:
            idx = text.find(name, start)
            if idx < 0:
                break
            if not any(mark in text[max(0, idx - 5):idx] for mark in _NEGATION_MARKERS):
                return True
            start = idx + len(name)
    return False


def _desc_from_design(plan: dict) -> str:
    """按**最终设计清单**重建一句话描述（只在 LLM 的文字与清单冲突时兜底用）。"""
    d = plan.get('design') if isinstance(plan.get('design'), dict) else {}
    segs: list[str] = []
    for group in ('main_flowers', 'fillers', 'foliage'):
        for flower in d.get(group) or []:
            if isinstance(flower, dict) and flower.get('name'):
                qty = flower.get('qty')
                segs.append(f"{flower['name']}×{qty}" if qty else str(flower['name']))
    colors = d.get('color_scheme') or []
    colors = '、'.join(str(c) for c in colors) if isinstance(colors, list) else str(colors)
    style = str(plan.get('style') or '').strip()
    return (f"{style}风格花束：花材共 {'、'.join(segs) or '以最终清单为准'}，"
            f"色调{colors or '自然'}，{d.get('packaging') or '花束'}包装。")


def _meaning_from_design(design: dict) -> str:
    """按**最终主花**的花语重建寓意文案（花语取自知识库，不编造）。"""
    langs: list[str] = []
    for flower in design.get('main_flowers') or []:
        if not isinstance(flower, dict):
            continue
        value = flower.get('flower_language') or []
        langs.extend(str(x) for x in (value if isinstance(value, list) else [value]) if x)
    return '、'.join(dict.fromkeys(langs)) or '美好心意'


def _scrub_excluded_mentions(plan: dict, exclude: set[str]) -> None:
    """清掉方案**文字字段**里对「被排除花材」的**正面提及**（否定表述保留）。

    为什么（2026-09-20 实测缺陷）：卡面上除了花材清单，还有一句话描述（desc）、
    制作步骤（diy_steps）、花语文案（meaning）。用料剔除了满天星，但文字里若还写着
    「配满天星」，用户看到的依然是自相矛盾 —— 和「数据残留」是同一个问题的另一面。
    """
    names = sorted(n for n in exclude if n)
    if not names:
        return
    d = plan.get('design') if isinstance(plan.get('design'), dict) else {}
    if _mentions_excluded(str(plan.get('desc') or ''), names):
        plan['desc'] = _desc_from_design(plan)
        logger.info('[设计] desc 里正面提到了被排除的花材 → 已按最终清单重建描述')
    if _mentions_excluded(str(d.get('meaning') or ''), names):
        d['meaning'] = _meaning_from_design(d)
        logger.info('[设计] meaning 里正面提到了被排除的花材 → 已按最终主花重建寓意')
    steps = plan.get('diy_steps')
    if isinstance(steps, list):
        kept = [s for s in steps if not _mentions_excluded(str(s), names)]
        if len(kept) != len(steps):
            plan['diy_steps'] = kept
            if isinstance(d.get('diy_steps'), list):
                d['diy_steps'] = kept
            logger.info('[设计] diy_steps 里提到被排除花材的步骤已移除（%d → %d 步）',
                        len(steps), len(kept))


def _merge_plan(baseline: dict, llm_plan: dict, exclude_flowers: set[str] | None = None) -> dict:
    """用 LLM 生成的语义字段覆盖 baseline；缺字段回落 baseline，保证 schema 完整不崩。

    baseline 由规则引擎 _build_plan 产出（机械字段齐全、花材真实），LLM 负责提升语义
    贴合度（选花/配色/文案/寓意）。两者合并既治本（理解模糊需求）又稳（结构永不错位）。
    """
    plan = copy.deepcopy(baseline)
    if not isinstance(llm_plan, dict):
        return plan
    # ⚠️ `estimated_price` / `budget_tier` 不在此列：价格与档位是**确定性字段**，
    # 由 _sync_plan_price 按最终明细统一写死，不能让模型自由发挥
    # （模型曾按用户预算写出与实际用料不符的价，导致「文案一个价、明细另一个价」）。
    for key in ('name', 'style', 'recipient', 'occasion', 'scene', 'desc', 'effect_prompt'):
        if llm_plan.get(key) not in (None, '', []):
            plan[key] = llm_plan[key]
    ld = llm_plan.get('design')
    if isinstance(ld, dict):
        bd = plan.setdefault('design', {})
        for key in ('main_flowers', 'fillers', 'foliage', 'color_scheme', 'packaging', 'meaning', 'notes', 'diy_steps', 'care_tips', 'card_message', 'budget_breakdown', 'difficulty', 'est_time', 'shelf_life', 'suitable_for', 'caution', 'mood_tags'):
            if key in ('fillers', 'foliage') and isinstance(ld.get(key), list):
                bd[key] = ld[key]
            elif ld.get(key) not in (None, '', []):
                bd[key] = ld[key]
        # LLM 精简输出后 main_flowers 里没有 role / flower_language（2026-09-20 延迟优化）。
        # 这两项都是**知识库静态属性**，从知识库补齐即可 —— 既省 token，
        # 又避免模型自己编花语（花语写错比不写更糟）。
        # ⚠️ demo 方案卡会在主花下面展示花语（demo/index.html:579），漏补会导致花语消失。
        for _group, _role in (('main_flowers', '主花'), ('fillers', '填充'), ('foliage', '叶材')):
            for _fl in bd.get(_group) or []:
                if not isinstance(_fl, dict) or not _fl.get('name'):
                    continue
                _fl.setdefault('role', _role)
                if not _fl.get('flower_language'):
                    _kf = _known_flower(str(_fl['name']))
                    if _kf:
                        _fl['flower_language'] = _kf.get('flower_language', []) or []
    # ★ 排除花材的**最终兜底**：baseline 干净 ≠ 合并结果干净（LLM 会覆盖）。
    # 必须在这里做——再晚就会跑在下游 diy_steps / 明细 / 价格重算的后面，
    # 变成「用料去掉了、步骤与报价还是旧的」。
    if exclude_flowers:
        _apply_flower_exclusions(plan, set(exclude_flowers), baseline.get('design'))
    if isinstance(ld, dict):
        # ⚠️ 花名来源必须是**已合并、已剔除排除项**的 `plan['design']`，不能是 LLM 原始 `ld`：
        # 否则「用料去掉了、diy_steps 与明细还是旧的」（2026-09-20 排除花材修复）。
        _merged = plan.get('design') if isinstance(plan.get('design'), dict) else {}

        def _merged_names(group: str) -> list[dict[str, Any]]:
            return [{'name': str(i.get('name'))} for i in (_merged.get(group) or [])
                    if isinstance(i, dict) and i.get('name')]

        real_main = _merged_names('main_flowers')
        real_fill = _merged_names('fillers')
        real_foli = _merged_names('foliage')
        if real_main:
            pk_name = ld.get('packaging') or plan.get('design', {}).get('packaging') or '花束'
            pkg = {'name': pk_name, 'id': 'PK_BOX' if '礼盒' in pk_name else 'PK_BOUQUET'}
            if ld.get('diy_steps') not in (None, '', []):
                plan['diy_steps'] = _cap_steps(ld['diy_steps'])
            else:
                plan['diy_steps'] = _build_diy_steps(real_main, real_fill, real_foli, ld.get('color_scheme') or [], pkg)
            if ld.get('budget_breakdown') not in (None, '', []):
                plan['budget_breakdown'] = ld['budget_breakdown']
            else:
                tier = _get_tier(plan.get('budget_num'), None)
                plan['budget_breakdown'] = _build_budget_breakdown(real_main, real_fill, real_foli, pkg, tier, plan.get('budget_num'), plan.get('stem_count'))
        if ld.get('card_message') not in (None, '', []):
            plan['card_message'] = ld['card_message']
        # 保持对外结构不变：2026-09-20 精简 prompt 后模型不再输出 design.diy_steps /
        # design.care_tips，但**以前这两项在 design 内层是有值的**，接入方可能直接读
        # （demo 是 `d.diy_steps || p.diy_steps` 双兜底，其他接入方不一定）。
        # 原则：只补齐、不删除 —— 绝不让已对接的字段从「有值」变成 undefined。
        for _k, _src in (('diy_steps', plan.get('diy_steps')),
                         ('care_tips', plan.get('care_tips'))):
            if _src and not bd.get(_k):
                bd[_k] = _src
    # ===== 硬性约束：用户明确的单一花材 / 支数，LLM 输出不得违背 =====
    _sf = plan.get('_single_flower')
    _sc = plan.get('stem_count')
    if _sf:
        sf_flower = _known_flower(_sf)
        sf_name = sf_flower.get('name', _sf) if sf_flower else _sf
        if _sc is not None:
            qty = int(_sc)
        else:
            _t = _get_tier(plan.get('budget_num'), None)
            qty = _TIER_MAIN_STEMS.get(_t.get('tier', 'T2'), 10)
        # 严格只保留该单一花材，清空被 LLM 混入的其他花/配材/叶材。
        plan['design']['main_flowers'] = [{'name': sf_name, 'role': '主花', 'flower_language': (sf_flower.get('flower_language', []) if sf_flower else []), 'qty': qty}]
        plan['design']['fillers'] = []
        plan['design']['foliage'] = []
        # notes 还原为规则引擎的真实说明：过滤掉含其它花材名的条目（可能是 LLM 残留或
        # 预算档模板举例），并补一条单花说明，保证方案里不再出现无关花材字样。
        _fl_terms = {t for t in _all_flower_terms() if t != sf_name and t != _sf}
        plan['design']['notes'] = [n for n in (baseline.get('design', {}).get('notes') or []) if not any(t in n for t in _fl_terms)]
        plan['design']['notes'].append(f"单一花材：仅用{sf_name}一种花材，不搭配配材/叶材。")
        # 寓意一并按该花材真实花语重建，避免 LLM 残留「康乃馨代表母爱」等其它花材文案。
        plan['design']['meaning'] = '、'.join(dict.fromkeys(sf_flower.get('flower_language', []))) if sf_flower else '美好心意'
        style_label = plan.get('style') or '韩式'
        pk_name = plan['design'].get('packaging') or '花束'
        meaning = plan['design'].get('meaning') or '美好心意'
        est = plan.get('estimated_price') or ''
        # 方案名也须同步：LLM 若起了「康乃馨花束」之类的名字，覆盖为纯该花材。
        plan['name'] = f"{style_label}·纯{sf_name}花束"
        # effect_prompt 不在此处设置：改由 _merge_plan 末尾「支数校正后」统一重建，
        # 否则单一花材 / 普通花束两条路径各写一套，支数一改就对不上。
        plan['desc'] = f"为你设计了一份纯{sf_name}花束：{sf_name}×{qty}，寓意{meaning}。{('预算' + str(est) + '。') if est else ''}"
        pkg = {'name': pk_name, 'id': 'PK_BOX' if '礼盒' in pk_name else 'PK_BOUQUET'}
        plan['diy_steps'] = _build_diy_steps(plan['design']['main_flowers'], [], [], plan['design'].get('color_scheme') or [], pkg)
        tier = _get_tier(plan.get('budget_num'), None)
        plan['budget_breakdown'] = _build_budget_breakdown(plan['design']['main_flowers'], [], [], pkg, tier, plan.get('budget_num'), plan.get('stem_count'))
    elif _sc is not None and plan['design'].get('main_flowers'):
        # 仅明确支数（非单一花材）：统一把主花支数设为用户指定数量。
        for fl in plan['design']['main_flowers']:
            fl['qty'] = int(_sc)
    _anchor_style(plan)
    plan = _enrich_plan_fees(plan)
    # ===== 支数一致性校正 =====
    # 最终支数已定（预算分配 / 单一花材硬约束都跑完了），此时把 LLM 写的文字里的
    # 数量对齐到方案真实支数，避免「desc 说 ×12、花材清单说 ×3」这种自相矛盾。
    _qmap = _design_qty_map(plan.get('design') or {})
    if _qmap:
        plan['desc'] = _sync_flower_qty(str(plan.get('desc') or ''), _qmap)
        if isinstance(plan.get('diy_steps'), list):
            plan['diy_steps'] = [_sync_flower_qty(str(s), _qmap) for s in plan['diy_steps']]
        _d = plan.get('design') or {}
        if isinstance(_d.get('diy_steps'), list):
            _d['diy_steps'] = [_sync_flower_qty(str(s), _qmap) for s in _d['diy_steps']]
        # 生图提示词也在此重建：支数必须取自**校正后的最终清单**。否则效果图会按
        # LLM 早期写的数量出图，与卡片里的花材清单自相矛盾（用户 2026-09-16 反馈）。
        plan['effect_prompt'] = _effect_prompt_from_design(
            plan.get('design') or {},
            plan.get('style') or '韩式',
            (plan.get('design') or {}).get('packaging') or '花束',
        )
    # 文字字段的排除兜底：数据剔了、文字里还写着「配满天星」同样是自相矛盾。
    # 放在支数校正**之后**——重建 desc 需要已定稿的 qty / 明细。
    if exclude_flowers:
        _scrub_excluded_mentions(plan, set(exclude_flowers))
    return plan

# L2：在设计调用里**顺带**要求模型输出它读到的结构化需求（零额外 LLM 调用）。
# 关键约束「没说的字段一律 null，不要猜」——否则"补召回"就退化成"猜值注入"。
_LLM_REQUIREMENT_HINT = (
    '\n【同时输出结构化需求】另请输出 "requirements" 对象，把用户原话里**明确读到**的送花需求结构化：'
    '{"recipient":"收花人（母亲/恋人/朋友/自己/长辈/宝宝 之一，未明确则 null）",'
    '"occasion":"场合（生日/母亲/父亲/节日/告白/婚礼/探病/道歉/毕业/乔迁/开业/升职/入职 之一，未明确则 null）",'
    '"budget":"预算数字（如 200；用户没提金额或大概价位就 null）",'
    '"style":"风格（S_KOREAN/S_NORDIC/S_VINTAGE/S_NATURAL/S_INS/S_JAPANESE 之一，未明确则 null）",'
    '"colors":["颜色（红/粉/白/香槟/紫/蓝/黄/橙/绿/多彩混合/亮），未明确则 []"],'
    '"mood":"情绪（温柔/温馨/浪漫/清新/热烈/活泼/高级/素雅/优雅/治愈/甜美 之一，未明确则 null）",'
    '"stem_count":"用户明确说出的支数，只填阿拉伯数字（用户说「11 朵」就填 11）；**用户没提就填 null**",'
    '"single_flower":"用户要求只用一种花时填花名（「纯红玫瑰」→ 红玫瑰），否则 null"}。'
    '严格按原话判断：**没有说的字段一律 null / []，绝不根据常识推测、也不要把示例里的数字当成默认值**'
    '——该对象会被系统用于校验（预算与支数会直接决定报价），填错比不填更糟。'
)


def design_with_llm(requirements: str, shop_id: str = '', session_requirement: FlowerRequirement | None = None) -> dict:
    """语义化设计：RAG 检索知识库 + DeepSeek 生成方案，规则引擎作兜底与结构补全。

    相对纯规则引擎，LLM 能理解「治愈系」「有故事感」「不按常理」等模糊/语义化需求，
    从知识库召回的真实花材中组织出更贴合的方案文案、选花与配色，而非套模板。

    shop_id 非空（用户从某家店铺进入）时，方案的原料范围被硬限定在该店铺在售清单内。
    """
    # 会话累积需求 + 本轮抽取：把「前面几轮说过的」送谁 / 场合 / 预算也带进规则基线。
    # 此前只按当前这条消息抽取，一旦 LLM 失败回退 baseline，方案就会缺跨轮补充的信息。
    req = accumulate(session_requirement, extract_requirement(requirements))
    # 用户在这句话里明确不要的花材（「不要满天星」）→ 规则基线直接排除。
    # ⚠️ 以前只有**改版**路径（revise_with_llm）解析排除，首轮设计完全不看，
    # 于是「帮我设计一束不要满天星的」照样给满天星（2026-09-20 实测）。
    exclude = set(_extract_feedback(requirements)['exclude'])
    baseline = _build_plan(req.to_legacy_dict(), exclude_flowers=exclude, shop_id=shop_id)
    if shop_id:
        baseline['shop_id'] = shop_id
    try:
        knowledge = _retrieve_for_design(requirements)
        # 用户显式约束（单一花材 / 支数）注入到设计 prompt，引导 LLM 不跑偏。
        # 注：req 已在函数开头按「会话累积 + 本轮」合并，此处不再重新抽取。
        hard_constraints: list[str] = []
        if req.single_flower:
            hard_constraints.append(f"用户明确要求『纯{req.single_flower}』单一花材：禁止混入任何其他花材、配材或叶材（不得出现康乃馨、非洲菊、满天星、尤加利等），design.fillers 与 design.foliage 必须为空数组，main_flowers 只能含「{req.single_flower}」。")
        if req.stem_count is not None:
            hard_constraints.append(f"用户明确要求主花 {req.stem_count} 支（朵），main_flowers 的 qty 必须为 {req.stem_count}。")
        if exclude:
            hard_constraints.append(_exclusion_rule(exclude))
        if shop_id:
            hard_constraints.append(_shop_scope_rule(shop_id))
        constraint_block = ('\n【硬性约束（必须严格遵守，违反即无效）】\n' + '\n'.join(hard_constraints)) if hard_constraints else ''
        # ⚠️ 只让模型输出**规则引擎做不到的语义部分**（方案名/文案/选花/配色/包装/寓意）。
        # 实测依据（2026-09-20 服务器实测）：LLM 耗时 ≈ 输出 token × 17~19ms，
        # **上下文长度几乎无影响**（塞到 2 万字首字仍只要 1.76s）。
        # 完整 schema 输出 793 token → 15.0s；精简到 424 token → 7.2s（省 52%）。
        # 其余字段（effect_prompt / diy_steps / care_tips / card_message / difficulty /
        # est_time / shelf_life / suitable_for / caution / mood_tags，以及
        # recipient / occasion / scene / role）**baseline 已算好**，
        # _merge_plan 是「LLM 缺则回落 baseline」→ 不写反而更快、质量不降。
        # ⚠️ effect_prompt 尤其要删：它在 _merge_plan 末尾会被 _effect_prompt_from_design
        # 按**校正后的支数**重建（卡片与生图必须同源），模型写了也是白烧 token。
        # ⚠️ flower_language 也不再要模型写（花语是知识库静态属性，模型写可能编造），
        # 由 _merge_plan 合并后从知识库补。
        system = '你是资深花艺设计师。依据用户需求与下方【知识库召回】设计一份花艺方案，只输出 JSON、不要额外解释。字段须严格为：{"name":方案名,"style":风格标签,"desc":一句话方案描述（含花材与支数，如「玫瑰×10 配满天星×3」）,"design":{"main_flowers":[{"name":花名,"qty":支数}],"fillers":[{"name":花名,"qty":支数}],"foliage":[{"name":叶材名,"qty":支数}],"color_scheme":[颜色],"packaging":包装名,"meaning":寓意文案}}。要求：花材必须从【候选花材】中选取真实名称；配色与风格须与知识库一致；每种花材务必给出具体支数 qty（按预算合理分配，主花 6-16 支、配材/叶材 1-4 支）；若用户未指定某维度，按花语与场景合理默认，不要留空。' + constraint_block
        # L2：让模型在同一次调用里顺带给出结构化需求（补规则漏抽的槽位）
        system = system + _LLM_REQUIREMENT_HINT
        user = f'用户需求：{requirements}\n\n{knowledge}'
        resp = call_llm([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], response_format={'type': 'json_object'})
        content = resp.choices[0].message.content
        llm_plan = json.loads(content)
        # L2 补召回：模型的 requirements 只补规则漏抽的槽位（fill-the-gap，不覆盖），
        # 补到的「硬约束」（单一花材 / 支数 / 预算档）落到 baseline 上，
        # 由 _merge_plan 据此校正模型输出——与 revise_with_llm 里
        # 「从原方案继承 _single_flower / stem_count」是同一套做法。
        from backend.config import settings
        if settings.DIY_LLM_REQUIREMENT_ENABLED:
            req_merged = merge_requirement(req, llm_plan.get('requirements') if isinstance(llm_plan, dict) else None)
            # ⚠️ 支数必须能在用户原话里对上才允许补（模型会把 prompt 里的示例值
            # 当默认值填回来 —— 详见 _reject_hallucinated_stem_count 的实测记录）
            req_merged = _reject_hallucinated_stem_count(req, req_merged, requirements)
            if req_merged.single_flower and not baseline.get('_single_flower'):
                baseline['_single_flower'] = req_merged.single_flower
            if req_merged.stem_count is not None and baseline.get('stem_count') is None:
                baseline['stem_count'] = req_merged.stem_count
            if req_merged.budget_num is not None and not baseline.get('budget_num'):
                baseline['budget_num'] = req_merged.budget_num
            if req_merged is not req:
                logger.info('[requirement] LLM 补召回 stem_count=%s single_flower=%s budget_num=%s colors=%s',
                            req_merged.stem_count, req_merged.single_flower, req_merged.budget_num, req_merged.colors)
        plan = _merge_plan(baseline, llm_plan, exclude_flowers=exclude)
        plan['plan_id'] = baseline['plan_id']
        plan['version'] = baseline.get('version', 1)
        plan['parent_id'] = baseline.get('parent_id')
        plan['diy'] = True
        return plan
    except Exception:
        logger.exception('[design] LLM 语义生成失败，回退规则引擎')
        return baseline

def _exclusion_rule(exclude: set[str]) -> str:
    """用户明确不要的花材 → 注入设计/改版 prompt 的硬性约束文案。

    为什么要有（2026-09-20 实测）：用户说「不要满天星」，改版后卡片配材里仍有
    「满天星×5」，模型自己都在文案里承认「清单里还残留了一条」。
    光靠「反馈明确要改的维度必须落实」这句泛化要求管不住否定式指令，
    所以这里把**具体花名**点名列出（与单一花材约束同样的处理方式）。
    ⚠️ 提示词只是第一道；``_apply_flower_exclusions`` 会在合并后**确定性剔除**，
    两道一起上才叫保险（模型不听话时兜底能兜住）。
    """
    return (f"用户明确不要这些花材：{'、'.join(sorted(exclude))}。"
            'main_flowers / fillers / foliage 都**不允许出现**它们（不要用同义写法绕开），'
            'desc 与 diy_steps 等文字里也不得提及，改用其他花材或直接留空。')


def _shop_scope_rule(shop_id: str) -> str:
    """店铺锁定场景下的原料约束文案（用户从某家店铺进入时注入设计/改版 prompt）。

    为什么重写（2026-09-17 线上实测）：原实现只让模型「自己去查该店在售花材」，
    结果模型**根本不查**（锁店 s001 的一轮里工具只有 generate_diy_plan /
    generate_effect_image，却用上了该店没有的「勿忘我」）；而且那句话本身把模型
    引向查 entity="plan" —— 商品不是花材，查回来也提取不出用料。
    现在改为**代码先把该店可提供花材查好**，把清单直接写进约束里，模型无需再查。
    """
    from agent.shop_materials import available_materials

    mats = available_materials(shop_id)
    if mats:
        listing = '、'.join(sorted(mats))
        return (f'本方案面向店铺 {shop_id}，该店在售商品用到的花材有：{listing}。'
                f'**优先从这份清单里选花材**，这样客户拿到方案就能在这家店配齐。'
                f'如果为了让效果更贴合需求，确实需要清单之外的花材，也可以用——'
                f'系统会自动标注哪些原料该店暂时没有，你照实说明即可，不要谎称该店能提供。')
    if mats is not None:
        # 查到了、但该店商品里提取不出任何花材 → 不要硬编清单，也别让模型乱承诺
        return (f'本方案面向店铺 {shop_id}。该店在售商品里没有可识别的花材用料信息，'
                f'请按通用花材正常设计，但**不要声称这些原料一定能在该店买到**。')
    # 查询失败 / 未配置数据源 → 结论未知，退回「不要编造」的弱约束
    return (f'本方案面向店铺 {shop_id}。请按通用花材正常设计，'
            f'但**不要声称这些原料一定能在该店买到**。')


def annotate_shop_materials(plan: dict, shop_id: str = '') -> dict:
    """按该店可提供花材，标注方案里哪些原料该店暂时没有。

    Capri 2026-09-17 决定：缺料**不自动替换**（硬换会破坏设计意图），只如实标注。
    因此这里只做标注，不改变 `design` 的花材选择：
    - 每种用料加 ``in_shop`` 布尔；
    - 方案顶层加 ``unavailable_materials``（该店没有的原料名列表），供模型在
      回复里如实说明、也供前端展示。

    ⚠️ 用 :func:`agent.shop_materials.available_materials` 的返回值区分三种情况：
    集合非空 = 有清单可判定；**None = 查询失败（未知）→ 一律不标注**，
    不能把「查不到」说成「该店没有」。

    Args:
        plan: 已生成的方案 dict（会被就地补充字段）。
        shop_id: 店铺 ID；留空时回退方案自带的 ``shop_id``。

    Returns:
        同一个 plan 对象（补充标注后）。
    """
    sid = str(shop_id or plan.get('shop_id') or '').strip()
    if not sid or not isinstance(plan, dict):
        return plan
    from agent.shop_materials import available_materials, materials_in_text

    mats = available_materials(sid)
    if not mats:
        return plan
    design = plan.get('design')
    if not isinstance(design, dict):
        return plan
    missing: list[str] = []
    for key in ('main_flowers', 'fillers', 'foliage'):
        for f in design.get(key) or []:
            if not isinstance(f, dict):
                continue
            name = str(f.get('name') or '').strip()
            if not name:
                continue
            hit = materials_in_text(name)
            # 知识库认识这个名字就按交集判；不认识（新花材）退回子串匹配
            ok = bool(hit & mats) if hit else any(a in name or name in a for a in mats)
            f['in_shop'] = ok
            if not ok:
                missing.append(name)
    if missing:
        plan['unavailable_materials'] = list(dict.fromkeys(missing))
        logger.info('[design] 方案用料 %d 项该店(%s)暂无：%s', len(missing), sid, '、'.join(missing))
    return plan


def design_diy_plan(requirements: str, shop_id: str = '', session_requirement: FlowerRequirement | None = None) -> dict:
    """设计一份结构化 DIY 花艺方案（RAG + LLM 语义生成，规则引擎兜底）。

    链路：RAG 检索知识库 → DeepSeek 生成语义化方案 → 规则引擎 _build_plan 补全结构/兜底。
    返回可供 UI 渲染、生图与下单承接的结构化 dict。
    shop_id 非空时，方案会带上「哪些原料该店暂时没有」的标注（见
    :func:`annotate_shop_materials`）——**保留花材本身，只标注，不自动替换**。
    """
    plan = design_with_llm(requirements, shop_id=shop_id, session_requirement=session_requirement)
    from agent.plan_validator import repair_plan
    plan = repair_plan(plan, session_requirement)
    plan = annotate_shop_materials(plan, shop_id)
    # 「复制用料清单」：必须在缺料标注之后生成，否则清单漏掉「该店暂无」提醒
    if isinstance(plan, dict):
        plan['copy_text'] = build_plan_copy_text(plan)
    return plan

def _revise_visual_fields(original: dict, feedback: str) -> dict | None:
    """局部视觉修订：输出仅应用获准字段，不经过重新选材/支数校正。"""
    return revise_visual_fields(original, feedback, call_model=call_llm,
                                effect_prompt=_effect_prompt_from_design,
                                copy_text=build_plan_copy_text)


def revise_with_llm(plan: str, feedback: str, shop_id: str = '') -> dict:
    """语义化改版：RAG 检索 + DeepSeek 基于已有方案与反馈调整，规则引擎兜底。

    反馈里明确要改的（预算/风格/色系/移除花材）必须落实；未提及维度保持原方案。
    shop_id 非空（或从原方案继承）时，改版后的原料仍限定在该店铺在售范围内。
    """
    original = _parse_plan(plan)
    if not original.get('plan_id') or not isinstance(original.get('design'), dict):
        return {'ok': False, 'error': '未找到完整的原方案，请先生成方案'}
    visual = _revise_visual_fields(original, feedback)
    if visual is not None:
        return visual
    feedback_dims = _extract_feedback(feedback)['dims']
    if 'budget' not in feedback_dims and re.search(r'便宜|省一点|降预算|加预算|贵一点|降低预算|提高预算', feedback):
        return {'ok': False, 'error': '请告诉我新的预算金额，以及是否允许调整花材数量；原方案已保留'}
    # 店铺锁定沿用原方案，避免改版后跳出该店在售范围。
    shop_id = shop_id or str(original.get('shop_id') or '')
    dims = _dims_from_plan(original)
    fb = _extract_feedback(feedback)
    # 排除项**跨轮继承**：用户上一轮说过「不要满天星」，这一轮只说「换个配色」时不能失效
    # （`_build_plan` 会把它持久化到 `plan['exclude_flowers']`，这里并集回来）。
    exclude = set(fb['exclude']) | {str(n) for n in (original.get('exclude_flowers') or [])}
    dims.update(fb['dims'])
    # 修订以实际上一版为底稿，不能重新选花构造一个替代底稿。
    import copy
    baseline = copy.deepcopy(original)
    baseline['plan_id'] = 'DIY_' + uuid.uuid4().hex[:12]
    baseline['version'] = int(original.get('version', 1)) + 1
    baseline['parent_id'] = original['plan_id']
    if 'budget' in fb['dims']:
        baseline['budget_num'] = float(fb['dims']['budget'])
    for key in ('task_id', 'poll', 'result_url', 'effect_image_url', 'image_url', 'image'):
        baseline.pop(key, None)
    if shop_id:
        baseline['shop_id'] = shop_id
    # 继承原方案的单一花材 / 支数约束，避免改版后跑偏。
    if original.get('_single_flower'):
        baseline['_single_flower'] = original['_single_flower']
    if original.get('stem_count') is not None:
        baseline['stem_count'] = original['stem_count']
    try:
        knowledge = _retrieve_for_design(f"{original.get('desc', '')} {feedback}")
        hard_constraints: list[str] = []
        if original.get('_single_flower'):
            hard_constraints.append(f"原方案为『纯{original['_single_flower']}』单一花材：除用户反馈明确要求加入其他花材外，必须保持单一花材，禁止混入康乃馨、非洲菊、满天星、尤加利等。")
        if original.get('stem_count') is not None:
            hard_constraints.append(f"原方案主花为 {original['stem_count']} 支，除非反馈要求改数量，否则 qty 保持 {original['stem_count']}。")
        if exclude:
            hard_constraints.append(_exclusion_rule(exclude))
        if shop_id:
            hard_constraints.append(_shop_scope_rule(shop_id))
        constraint_block = ('\n【硬性约束（必须严格遵守，违反即无效）】\n' + '\n'.join(hard_constraints)) if hard_constraints else ''
        # 同 design_with_llm：只让模型改**语义部分**，其余字段由 baseline + _merge_plan 补。
        # 依据见 design_with_llm 处的实测记录（输出 token 数才是耗时主因）。
        system = '你是资深花艺设计师。基于【已有方案】与【用户反馈】调整出一版新方案，只输出 JSON、不要额外解释。字段须严格为：{"name":方案名,"style":风格标签,"desc":一句话方案描述,"design":{"main_flowers":[{"name":花名,"qty":支数}],"fillers":[{"name":花名,"qty":支数}],"foliage":[{"name":叶材名,"qty":支数}],"color_scheme":[颜色],"packaging":包装名,"meaning":寓意文案}}。要求：反馈明确要改的维度必须落实；花材从知识库真实名称选；未提及的维度保持原方案，不要随意改动。' + constraint_block
        user = f'已有方案：{json.dumps(original, ensure_ascii=False)}\n用户反馈：{feedback}\n\n{knowledge}'
        resp = call_llm([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], response_format={'type': 'json_object'})
        llm_plan = json.loads(resp.choices[0].message.content)
        if not isinstance(llm_plan, dict) or not isinstance(llm_plan.get('design'), dict):
            raise ValueError('invalid revision design')
        # 未授权视觉字段使用原稿；费用和图片提示随后基于这些字段重算。
        requested = re.sub(r'(?:保留|保持)[^，。；,;]*|[^，。；,;]*(?:不变|不改)', '', feedback)
        for field, pattern in (('packaging', r'包装|包材'), ('color_scheme', r'配色|色系|色调|颜色')):
            if not re.search(pattern, requested):
                llm_plan['design'][field] = copy.deepcopy(original['design'].get(field))
        if not re.search(r'风格|韩式|北欧|法式|日式|复古|自然风', requested):
            llm_plan['style'] = original.get('style', '')
        new_plan = _merge_plan(baseline, llm_plan, exclude_flowers=exclude)
        new_plan['plan_id'] = baseline['plan_id']
        new_plan['version'] = original.get('version', 1) + 1
        new_plan['parent_id'] = original.get('plan_id')
        new_plan['diy'] = True
        if shop_id:
            new_plan['shop_id'] = shop_id
        from agent.plan_validator import repair_plan
        new_plan = repair_plan(new_plan)
        # 用户明确锁定的维度在合并、定价和修复之后再次校验。
        # 不把不满足要求的方案落库，也不让模型以解释代替约束。
        before = original.get('design', {})
        after = new_plan.get('design', {})
        preserve_materials = bool(re.search(r'(?:保留|保持|不改|不换).{0,12}花材|花材.{0,6}(?:不变|保留|保持)', feedback))
        preserve_qty = bool(re.search(r'(?:保留|保持|不改).{0,8}(?:数量|支数)|(?:数量|支数).{0,6}不变', feedback))
        preserve_total = bool(re.search(r'(?:总支数|总数量|总数).{0,6}(?:不变|保持|保留)|(?:保持|保留).{0,6}(?:总支数|总数量|总数)', feedback))
        preserve_each = bool(re.search(r'(?:每种|各自|各花材).{0,8}(?:数量|支数).{0,6}不变', feedback))
        def signature(design, quantities=False):
            return tuple((group, tuple(sorted((str(f.get('name', '')), f.get('qty') if quantities else 0)
                         for f in design.get(group, []) if isinstance(f, dict))))
                         for group in ('main_flowers', 'fillers', 'foliage'))
        if preserve_materials and signature(before) != signature(after):
            return {'ok': False, 'error': '本次调整未能保留原花材，原方案已保留，请明确允许更换哪些花材'}
        def total(design):
            return sum(f.get('qty', 0) for group in ('main_flowers', 'fillers', 'foliage')
                       for f in design.get(group, []) if isinstance(f, dict) and isinstance(f.get('qty'), (int, float)))
        if preserve_total and total(before) != total(after):
            return {'ok': False, 'error': '本次调整未能保留总支数，原方案已保留，请确认是否允许调整数量'}
        if (preserve_each or (preserve_qty and not preserve_total)) and signature(before, True) != signature(after, True):
            return {'ok': False, 'error': '本次调整未能保留原支数，原方案已保留，请确认是否允许调整数量'}
        ceiling = baseline.get('budget_num')
        if isinstance(ceiling, (int, float)) and isinstance(new_plan.get('price'), (int, float)) and new_plan['price'] > ceiling:
            return {'ok': False, 'error': f'调整后的估价约 {new_plan["price"]} 元，超过预算 {ceiling:g} 元；原方案已保留，请确认减量或提高预算'}
        return new_plan
    except Exception:
        logger.exception('[revise] LLM 语义改版失败，保留原方案')
        return {'ok': False, 'error': '本次修改未完成，原方案已保留，请重试'}

_CONFIRM_VALUES = ('confirm', 'reject', 'none')
_IMAGE_VALUES = ('want', 'decline', 'none')

_CONFIRMATION_PROP = {'type': 'string', 'enum': ['confirm', 'reject', 'none'], 'description': '用户本轮是否在回应你上一轮的**提议/方案**：confirm=认可并接受（如「好」「就这个」「可以」）；reject=不接受（如「不行」「换一个」「不要这个」）；none=本轮不涉及确认（新提问、新需求、闲聊）。注意「这个方案不行」「不是这个」「不好看」都是 reject，不要被其中「行/是/好」这些字骗到。'}
_IMAGE_PROP = {'type': 'string', 'enum': ['want', 'decline', 'none'], 'description': '用户本轮对**效果图/生图**的态度：want=想要图（如「出个效果图」「看看长什么样」「好，生成吧」）；decline=明确不要图（如「不用效果图」「别生成图」「算了不出图」）；none=没提图片的事。'}
_ALTERNATIVE_PROP = {'type': 'boolean', 'description': '用户是否想**换一批 / 再看别的**（如「还有别的吗」「换一个风格」「再看看其他方案」「太贵了，有便宜点的吗」）。没有这个意思就填 false。'}
# L3 澄清式追问：把「还缺哪些关键信息」交给模型判断（它读得到完整上下文），
# agent 侧据此确定性地先追问、而不是让模型硬编一个猜出来的方案。
MISSING_SLOT_VALUES = ('recipient', 'occasion', 'budget', 'style', 'colors')
_MISSING_PROP = {'type': 'array', 'items': {'type': 'string', 'enum': list(MISSING_SLOT_VALUES)}, 'description': '**本轮你若打算直接给出花束方案，请列出用户尚未提供的关键信息**（可多项；信息已足够就填空数组 []）：recipient=送给谁、occasion=什么场合、budget=预算、style=风格偏好、colors=颜色偏好。注意：只有「送花对象 / 场合 / 预算」真的不知道时才列出前三个——用户说「随便 / 你决定 / 直接来一束」时视为已授权你决定，不要列。'}


def normalize_missing_slots(raw: Any) -> list[str]:
    """把 missing 归一为白名单槽位列表（去重、保序、最多 3 个）。

    模型偶发会写出中文字段名（「预算」）或编造槽位，这里一律过滤掉——
    agent 侧只认标准枚举，避免误触发追问。
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        val = item.strip().lower() if isinstance(item, str) else ''
        if val in MISSING_SLOT_VALUES and val not in out:
            out.append(val)
    return out[:3]


@register_tool(name='respond_to_user', description='当你准备好向用户输出本轮最终回复时，必须调用该工具结束本轮对话。携带：reply（自然语言回复）、ui（UI 动作类型）、data（按 ui 类型填充）、stage（协商后的下一业务阶段）、intent（你判断的用户本轮真实意图）。另需给出本轮的结构化理解：confirmation / image / wants_alternative —— 系统用它们决定是否确认方案、是否生成效果图、是否换一批，务必按用户真实语义填写，不确定就填 none/false。', parameters={'type': 'object', 'properties': {'reply': {'type': 'string', 'description': '给用户的自然语言回复'}, 'ui': {'type': 'string', 'enum': [e.value for e in UIType], 'description': '小程序渲染的 UI 动作类型'}, 'data': {'type': 'object', 'description': '按 ui 类型约定的结构化数据'}, 'stage': {'type': 'string', 'description': '下一业务阶段，如 analyze/select_mode/view_plan/diy_design/image_gen/shop_recommend/done'}, 'intent': {'type': 'string', 'enum': ['buying', 'qa', 'chitchat', 'design', 'other'], 'description': '用户本轮真实意图：buying=有购买/挑选花束的明确意图；qa=问花卉/花艺知识或咨询（花期/养护/寓意/送什么花好）；chitchat=纯闲聊寒暄；design=要 DIY 定制专属花束；other=其他。判定依据是用户『想干什么』，不是本轮是否调了工具。'}, 'confirmation': _CONFIRMATION_PROP, 'image': _IMAGE_PROP, 'wants_alternative': _ALTERNATIVE_PROP, 'missing': _MISSING_PROP}, 'required': ['reply', 'ui', 'data', 'stage']}, tags=['meta'])
def respond_to_user(reply: str='', ui: str='text', data: dict | None=None, stage: str='analyze', intent: str='other', confirmation: str='none', image: str='none', wants_alternative: bool=False, missing: list[str] | None=None) -> dict[str, Any]:
    """终结工具：模型以此结束本轮，参数由 agent 提取并校验后返回前端。

    confirmation / image / wants_alternative / missing 是「对话理解」的结构化信号：
    由 LLM 判定、agent 侧消费（LLM 理解为主，关键词仅在其缺失/非法时兜底）。
    这里做一次白名单归一，非法值降级为 none/false/[]，保证下游拿到的一定是合法枚举。

    Args:
        missing: L3 用——本轮若打算直接出方案，用户还缺哪些关键信息
            （recipient / occasion / budget / style / colors 的白名单子集）。

    Returns:
        供 agent 直接消费的参数字典（含归一后的结构化信号）。
    """
    if intent not in ('buying', 'qa', 'chitchat', 'design', 'other'):
        intent = 'other'
    if confirmation not in _CONFIRM_VALUES:
        confirmation = 'none'
    if image not in _IMAGE_VALUES:
        image = 'none'
    return {'reply': reply, 'ui': ui, 'data': data or {}, 'stage': stage, 'intent': intent, 'confirmation': confirmation, 'image': image, 'wants_alternative': bool(wants_alternative), 'missing': normalize_missing_slots(missing)}


@register_tool(name='show_plan_card', description='标准方案卡片输出工具。用于展示现成方案或 DIY 方案，调用时只需传 plans 列表，工具会自动包装为 plan_card 并结束本轮对话。', parameters={'type': 'object', 'properties': {'reply': {'type': 'string', 'description': '给用户的自然语言说明。⚠️ 请**第一个**写这个字段：模型按参数顺序生成，reply 在前用户才能立刻看到文字（实测 reply 在后时首字要等 20s+）'}, 'plans': {'type': 'array', 'description': '方案卡片列表，每项应符合 plan_card 约定'}, 'stage': {'type': 'string', 'description': '下一业务阶段，默认 view_plan 或 diy_design'}, 'intent': {'type': 'string', 'enum': ['buying', 'qa', 'chitchat', 'design', 'other'], 'description': '用户本轮真实意图'}, 'confirmation': _CONFIRMATION_PROP, 'image': _IMAGE_PROP, 'wants_alternative': _ALTERNATIVE_PROP, 'missing': _MISSING_PROP}, 'required': ['reply', 'plans']}, tags=['meta'])
def show_plan_card(plans: list[dict] | None = None, reply: str='', stage: str='view_plan', intent: str='design', confirmation: str='none', image: str='none', wants_alternative: bool=False, missing: list[str] | None=None) -> dict[str, Any]:
    """方案卡片终结工具：统一输出 plan_card。

    与 respond_to_user 保持同一套结构化信号契约（confirmation / image /
    wants_alternative / missing），便于 agent 侧统一消费，不必按工具分支处理。
    """
    if intent not in ('buying', 'qa', 'chitchat', 'design', 'other'):
        intent = 'design'
    if stage not in ('view_plan', 'diy_design', 'plan_confirm', 'select_mode', 'analyze'):
        stage = 'view_plan'
    if confirmation not in _CONFIRM_VALUES:
        confirmation = 'none'
    if image not in _IMAGE_VALUES:
        image = 'none'
    return {'reply': reply, 'ui': UIType.PLAN_CARD.value, 'data': {'plans': plans or []}, 'stage': stage, 'intent': intent, 'confirmation': confirmation, 'image': image, 'wants_alternative': bool(wants_alternative), 'missing': normalize_missing_slots(missing)}


@register_tool(name='show_options', description='给用户一组可点选的选项（前端渲染成按钮），需要用户从几个方向里挑一个时用它——比如「想要哪种风格 / 哪个色系」「要不要继续调整」「换一批往哪个方向换」。传 options（每项一个简短标签）即可，工具会自动包装成选项列表并结束本轮对话。', parameters={'type': 'object', 'properties': {'reply': {'type': 'string', 'description': '给用户的自然语言说明（说明你为什么让他选，不要只写「请选择」）。⚠️ 请**第一个**写这个字段（模型按参数顺序生成，reply 在前用户才能立刻看到文字）'}, 'options': {'type': 'array', 'items': {'type': 'string'}, 'description': '选项列表，每项一个简短标签（建议 2-4 项，每项不超过 12 字），如 ["温柔韩式","清新自然","复古法式"]'}, 'stage': {'type': 'string', 'description': '下一业务阶段，默认 analyze'}, 'intent': {'type': 'string', 'enum': ['buying', 'qa', 'chitchat', 'design', 'other'], 'description': '用户本轮真实意图'}, 'confirmation': _CONFIRMATION_PROP, 'image': _IMAGE_PROP, 'wants_alternative': _ALTERNATIVE_PROP, 'missing': _MISSING_PROP}, 'required': ['reply', 'options']}, tags=['meta'])
def show_options(options: list | None=None, reply: str='', stage: str='analyze', intent: str='other', confirmation: str='none', image: str='none', wants_alternative: bool=False, missing: list[str] | None=None) -> dict[str, Any]:
    """选项列表终结工具：把「让用户挑一个」变成显式的工具调用。

    为什么单独成工具（2026-09-16）：此前「出选项」只能靠
    ``respond_to_user(ui="dialog_options", data={"options":[...]})`` —— 模型得自己拼
    data 结构，既容易拼错，也**想不到有这个能力**（能力藏在参数里 = 对模型不可见）。
    独立成工具后，模型在工具清单里就能看到「我可以给用户出选项」，从而在需要时主动用。

    Args:
        options: 选项标签列表；兼容字符串与 ``{label, value, hint}`` 两种写法。
        reply: 给用户的自然语言说明。
        stage: 下一业务阶段。
        intent: 用户本轮真实意图。
        confirmation / image / wants_alternative / missing: 与 ``respond_to_user`` 同契约。

    Returns:
        与 ``respond_to_user`` 同构的参数字典（``ui=dialog_options``）。
    """
    norm: list[dict[str, str]] = []
    for o in options or []:
        if isinstance(o, dict):
            label = str(o.get('label') or o.get('value') or '').strip()
            if not label:
                continue
            item = {'label': label, 'value': str(o.get('value') or label).strip()}
            if o.get('hint'):
                item['hint'] = str(o['hint']).strip()
            norm.append(item)
        elif isinstance(o, str) and o.strip():
            label = o.strip()
            norm.append({'label': label, 'value': label})
    if intent not in ('buying', 'qa', 'chitchat', 'design', 'other'):
        intent = 'other'
    if confirmation not in _CONFIRM_VALUES:
        confirmation = 'none'
    if image not in _IMAGE_VALUES:
        image = 'none'
    return {'reply': reply, 'ui': UIType.DIALOG_OPTIONS.value, 'data': {'options': norm}, 'stage': stage, 'intent': intent, 'confirmation': confirmation, 'image': image, 'wants_alternative': bool(wants_alternative), 'missing': normalize_missing_slots(missing)}
