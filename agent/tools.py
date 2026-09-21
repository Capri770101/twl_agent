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
    - 显式 plan_id：仅在命中会话最新 DIY 方案（plan_id 匹配或 DIY_ 前缀）时返回；
      平台商品（现有方案）已无本地镜像，由 platform_db_query_entity 查询后按需传递。
    - 解析结果与用户、会话绑定，不再依赖进程级全局状态（并发安全）。
    """
    uid = (_context or {}).get('user_id', '')
    sid = (_context or {}).get('session_id', '')
    if plan in ('latest', 'latest_diy', '', None):
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
        if diy and (diy.get('plan_id') == plan or str(plan).startswith('DIY_')):
            return diy
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
    prompt = (plan_obj.get('effect_prompt') or plan_obj.get('desc') or plan_obj.get('name') or '花束').strip()
    if not prompt:
        return json.dumps({'error': '方案缺少可生图的描述信息'}, ensure_ascii=False)
    task_id = await tasks.create_image_task(prompt, user_id=(_context or {}).get('user_id'))
    result: dict[str, Any] = {'task_id': task_id, 'poll': f'/tasks/{task_id}'}
    try:
        st = await tasks.get_image_task(task_id, user_id=(_context or {}).get('user_id'))
        if st.get('status') == 'done' and st.get('result_url'):
            result['result_url'] = st['result_url']
    except Exception:
        logger.debug('[tools] 生图任务即时查询失败 task_id=%s', task_id)
    return json.dumps(result, ensure_ascii=False)

@register_tool(name='retrieve_knowledge', description='检索花卉 DIY 知识库：花材(花语/色系/季节/价格档/搭配性)、风格体系、搭配规则、预算映射、包装器型、鲜切花养护(醒花/剪根/换水/保鲜/延长花期)、商家智库（店铺的风格/擅长场景/价位/服务/卖点）。设计花艺方案前、或用户询问鲜花怎么保养时调用，以获取可靠的领域知识避免凭空编造；找店铺用 shop 域，问养护/保鲜用 care 域。', parameters={'type': 'object', 'properties': {'domain': {'type': 'string', 'description': '检索域：flower(花材) | style(风格) | pairing(搭配规则) | budget(预算) | packaging(包装) | care(鲜切花养护/保鲜) | shop(商家智库) | scene(场景) | proven(用户验证过的实战方案) | all(全部)'}, 'query': {'type': 'string', 'description': '关键词或自然语言，如 母亲/生日/北欧/200元/怎么养得久/能做婚礼布置的店'}}, 'required': ['domain', 'query']}, tags=['knowledge'])
def retrieve_knowledge(domain: str, query: str) -> str:
    """检索知识库，返回相关条目 JSON。"""
    return json.dumps(query_knowledge(domain, query), ensure_ascii=False)

_RECIPIENT_KW = {'妈妈': '母亲', '母亲': '母亲', '妈': '母亲', '娘': '母亲', '恋人': '恋人', '女朋友': '恋人', '男朋友': '恋人', '老婆': '恋人', '老公': '恋人', '对象': '恋人', '爱人': '恋人', '男友': '恋人', '女友': '恋人', '先生': '恋人', '丈夫': '恋人', '朋友': '朋友', '闺蜜': '朋友', '兄弟': '朋友', '同事': '朋友', '姐妹': '朋友', '自己': '自己', '悦己': '自己', '我': '自己', '长辈': '长辈', '老人': '长辈', '父母': '长辈', '领导': '长辈', '上司': '长辈', '老板': '长辈', '老师': '长辈', '宝宝': '宝宝', '婴儿': '宝宝', '新生儿': '宝宝'}
_OCCASION_KW = {'生日': '生日', '庆祝': '生日', '母亲节': '母亲', '父亲节': '父亲', '节': '节日', '告白': '告白', '表白': '告白', '纪念日': '告白', '求婚': '告白', '婚礼': '婚礼', '结婚': '婚礼', '领证': '婚礼', '探病': '探病', '生病': '探病', '康复': '探病', '住院': '探病', '道歉': '道歉', '对不起': '道歉', '抱歉': '道歉', '毕业': '毕业', '乔迁': '乔迁', '开业': '开业', '升职': '升职', '入职': '入职'}
_STYLE_KW = {'韩式': 'S_KOREAN', '韩系': 'S_KOREAN', '北欧': 'S_NORDIC', '简约': 'S_NORDIC', '极简': 'S_NORDIC', '复古': 'S_VINTAGE', '古典': 'S_VINTAGE', '港风': 'S_VINTAGE', '中古': 'S_VINTAGE', '自然': 'S_NATURAL', '野趣': 'S_NATURAL', '田园': 'S_NATURAL', 'ins': 'S_INS', 'ins风': 'S_INS', '网红': 'S_INS', '奶油风': 'S_INS', '法式': 'S_INS', '日式': 'S_JAPANESE', '禅': 'S_JAPANESE', '日系': 'S_JAPANESE'}
_COLOR_KW = {'红': '红', '粉': '粉', '白': '白', '香槟': '香槟', '紫': '紫', '蓝': '蓝', '黄': '黄', '橙': '橙', '绿': '绿', '多彩': '多彩混合', '亮': '亮', '鲜艳': '亮', '缤纷': '多彩混合', '粉嫩': '粉', '浅粉': '粉', '桃红': '粉', '正红': '红', '酒红': '红', '橘': '橙', '鹅黄': '黄', '天蓝': '蓝', '湖蓝': '蓝', '香槟色': '香槟', '五彩': '多彩混合', '撞色': '多彩混合'}
_MOOD_KW = {'温柔': '温柔', '温馨': '温馨', '浪漫': '浪漫', '清新': '清新', '热烈': '热烈', '活泼': '活泼', '高级': '高级', '素雅': '素雅', '优雅': '优雅', '莫兰迪': '素雅', '马卡龙': '清新', '小清新': '清新', '轻奢': '高级', '低调': '素雅', '治愈': '治愈', '安静': '素雅', '甜美': '甜美', '氛围感': '优雅', '高级感': '高级'}
_BUDGET_ORAL = {'一两百': 150, '一二百': 150, '小几百': 200, '两三百': 250, '二三百': 250, '三四百': 350, '三五百': 400, '五六百': 550, '七八百': 750, '千把块': 1000, '一千': 1000, '一两千': 1500, '两三千': 2500}

# 金额数量级单位：用户常说「预算5万」「三千左右」。丢掉单位会差 3~4 个数量级
# （实测「预算5万」曾被解析成 5 元），所以数字模式必须把它一起捕获。
_MAGNITUDE_UNITS: dict[str, float] = {
    '万': 1e4, '萬': 1e4, 'w': 1e4, 'W': 1e4,
    '千': 1e3, 'k': 1e3, 'K': 1e3,
    '百': 1e2,
}
# 「金额数字」模式：阿拉伯数字（可带小数）+ 可选数量级单位。两处 group：数字 / 单位。
_AMOUNT_PAT = r'(\d{1,6}(?:\.\d{1,2})?)\s*([万千百wWkK])?'

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

def _get_tier(budget_num: int | None, scene_anchor: str | None) -> dict:
    """解析预算档：显式预算优先 → 场景锚点 → 默认「精致/送礼」档。"""
    all_tiers = query_knowledge('budget', '')['results']
    if budget_num is not None:
        t = next((t for t in all_tiers if t['range'][0] <= budget_num <= t['range'][1]), None)
        if t:
            return t
    if scene_anchor:
        t = next((t for t in all_tiers if t['tier'] == scene_anchor), None)
        if t:
            return t
    return all_tiers[1]

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

def _parse_plan(plan: str) -> dict:
    """尽力解析传入的方案（可能是 JSON 字符串或含 JSON 的文本）。"""
    if isinstance(plan, dict):
        return plan
    text = plan.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {}

def _dims_from_plan(plan: dict) -> dict[str, str]:
    """从已有方案反推设计维度，供迭代时复用。"""
    dims: dict[str, str] = {}
    if plan.get('recipient'):
        dims['recipient'] = plan['recipient']
    if plan.get('occasion'):
        dims['occasion'] = plan['occasion']
    if plan.get('style_id'):
        dims['style'] = plan['style_id']
    if plan.get('substyle_id'):
        dims['substyle'] = plan['substyle_id']
    if plan.get('scene_id'):
        dims['scene'] = plan['scene_id']
    if plan.get('budget_num') is not None:
        dims['budget'] = str(plan['budget_num'])
    elif plan.get('budget_tier'):
        for t in query_knowledge('budget', '')['results']:
            if t['label'] == plan['budget_tier']:
                dims['budget'] = str(t['range'][0])
                break
    design = plan.get('design', {})
    main = [m['name'] for m in design.get('main_flowers', [])]
    if main:
        dims['_keep_main'] = ','.join(main)
    return dims

def _extract_feedback(feedback: str) -> dict[str, Any]:
    """解析自然语言反馈为调整指令：维度覆盖 + 需移除花材集合。"""
    import re
    dims: dict[str, str] = {}
    exclude: set[str] = set()
    m = re.search('(\\d{2,5})\\s*(?:元|块|块钱)?', feedback)
    if m:
        dims['budget'] = m.group(1)
    elif any(k in feedback for k in ('便宜', '低价', '省', '预算低', '降档')):
        dims['budget'] = '120'
    elif any(k in feedback for k in ('高档', '贵一点', '升级', '好一点', '加预算')):
        dims['budget'] = '500'
    for kw, val in _STYLE_KW.items():
        if kw in feedback:
            dims['style'] = val
            break
    for kw, val in _COLOR_KW.items():
        if kw in feedback:
            dims['color'] = val
            break
    for kw, val in _MOOD_KW.items():
        if kw in feedback:
            dims['mood'] = val
            break
    for name in _get_all_flower_names():
        if any(seg in feedback for seg in (f'不要{name}', f'去掉{name}', f'别用{name}', f'换掉{name}', f'去掉{name}花')):
            exclude.add(name)
    return {'dims': dims, 'exclude': exclude}
_RELATIONSHIP_MAP = {'母亲': '亲子', '恋人': '情侣', '朋友': '朋友', '自己': '自用', '长辈': '长辈/同事', '宝宝': '亲子'}

def _extract_budget(text: str) -> tuple[str | None, float | None, float | None, float | None]:
    """从文本抽预算：返回 (口语锚点, 精确金额, 区间下界, 区间上界)。

    精确金额与旧 _extract 的 dims['budget'] 保持一致（如「两三百」→ 250），
    区间按 ±20% 推导，供检索时做软过滤。

    ⚠️ **必须识别数量级单位（万 / 千 / 百 / w / k）**。线上实测（2026-09-18）：
    「预算5万」被解析成 **5 元**、「1.5万」被解析成 **1 元**（旧正则 `\\d{1,5}` 只抓数字、
    把「万」直接丢掉），「三万」「五千」这类中文写法**完全抽不到**。
    金额差 4 个数量级会让方案档位、报价、预算校验全错。
    """
    anchor: str | None = None
    for oral, num in _BUDGET_ORAL.items():
        if oral in text:
            text = text.replace(oral, f' {num} ')
            anchor = oral
            break

    # 必须有明确的价格信号才算预算：货币单位 / 预算 / 价格 / 「X元左右」。
    # 仅紧跟 朵/支/束 的数字（如「11 朵」）一律不当预算，避免把数量误判成金额。
    for pat in (
        re.compile(_AMOUNT_PAT + r'\s*(?:元|块|块钱|rmb|¥|刀)', re.I),
        re.compile(r'(?:预算|价格)\s*' + _AMOUNT_PAT),
        re.compile(_AMOUNT_PAT + r'\s*(?:预算|价格)'),   # 「5w预算」这类后置写法
        re.compile(_AMOUNT_PAT + r'\s*元左右'),
    ):
        m = pat.search(text)
        if m:
            num = float(m.group(1)) * _MAGNITUDE_UNITS.get(m.group(2) or '', 1.0)
            return (anchor, num, round(num * 0.8), round(num * 1.2))

    # 中文数字 + 数量级：「三百元」「预算三万」—— 阿拉伯正则抓不到。
    # ⚠️ 必须带**价格信号**才认（货币单位或「预算/价格」），否则「两万朵玫瑰」会被当成预算。
    for pat in (
        re.compile(r'([一二两三四五六七八九十]{1,3})\s*([万千百])\s*(?:元|块|块钱)'),
        re.compile(r'(?:预算|价格)\s*([一二两三四五六七八九十]{1,3})\s*([万千百])'),
    ):
        cm = pat.search(text)
        if cm:
            base = _cn_to_int(cm.group(1))
            if base:
                num = float(base) * _MAGNITUDE_UNITS[cm.group(2)]
                return (anchor, num, round(num * 0.8), round(num * 1.2))
    return (anchor, None, None, None)

_CN_DIGIT = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}


def _cn_to_int(raw: str) -> int | None:
    """中文数字 → 整数（支持 1–99 的常见写法：两 / 九 / 十 / 十一 / 二十 / 二十五）。

    Args:
        raw: 中文数字串。

    Returns:
        对应整数；无法解析返回 None。
    """
    s = (raw or '').strip()
    if not s:
        return None
    if '十' not in s:
        return _CN_DIGIT.get(s)
    left, _, right = s.partition('十')
    tens = 1 if left == '' else _CN_DIGIT.get(left)
    ones = 0 if right == '' else _CN_DIGIT.get(right)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


def _extract_stem_count(text: str) -> int | None:
    """抽取**用户明确说出**的花材支数：『11 朵』『9支』『十一朵』『一打』。

    仅当数量紧跟 朵/支/枝/根/头 才认定（避免把年份、金额误当支数）；
    中文数字支持个位与十位（两朵 / 九支 / 十朵 / 十一朵 / 二十五支）。

    ⚠️ **「一束」不在此列**（2026-09-18 修正，勿回退）：
    它是**容器 / 单位量词**，不是支数 —— 用户说「来一束花，预算 300」时并不关心几支。
    原实现把「一束」硬映射成 11 支，而支数是**强约束**（`_alloc_stems` 里优先级最高、
    会跳过「按预算反推」），实测把一束 300 元的方案算成 **141 元**（只有 6 支花）；
    模型察觉方案明显不合预算，又自己调一轮 `revise_diy_plan` 去救 ——
    **既报错价、又白烧一整轮 LLM**（单轮 DIY 因此从 ~30s 涨到 60~150s）。
    官网页（`client_payload`）早就有「一束让位预算」的修正，主链路一直缺这条，
    现在从**源头**统一，两条链路行为一致。
    「一打 = 12 支」保留：它是明确的计数单位。
    """
    m = re.search(r'(\d{1,3})\s*(?:朵|支|枝|根|头)', text)
    if m:
        return max(1, min(int(m.group(1)), 999))
    if re.search(r'一\s*打', text):
        return 12
    cm = re.search(r'([一二两三四五六七八九]?十[一二三四五六七八九]?|[一二两三四五六七八九])\s*(?:朵|支|枝|根|头)', text)
    if cm:
        val = _cn_to_int(cm.group(1))
        if val:
            return max(1, min(val, 999))
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


def _safe_float(value: Any) -> float | None:
    """宽松转 float（bool 视为无效，避免 True→1 这种荒唐值）。"""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    """宽松转 int（接受 '11' / 11.0；bool 视为无效）。"""
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


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
# 主花支数的**兜底**基准（按预算档）。
# ⚠️ 正常路径不用它：真实支数由「（预算 − 基础费）÷ 单品综合成本」算出（见 _alloc_stems）。
# 它只在「用户没给预算、档位也没有区间」时兜底，避免方案空掉。
_TIER_MAIN_STEMS = {'T1': 6, 'T2': 8, 'T3': 10, 'T4': 14, 'T5': 18, 'T6': 24}
# 单束主花支数上限：防极端预算（如「预算 5 万」）算出上千支。99 也贴合「久久」的寓意习惯。
_MAX_MAIN_STEMS = 99

def _known_flower(name: str) -> dict:
    """按花名查知识库花卉（含别名匹配），查不到返回空 dict。"""
    if not name:
        return {}
    all_fl = query_knowledge('flower', '')['results']
    for f in all_fl:
        if f['name'] == name or name in f.get('aliases', []):
            return f
    return {}

def _price_table(shop_id: str = ''):
    """取该店（或全网）的花材价表 —— 报价的唯一来源。

    定价已从「代码里的三条常数」改为**从平台在售商品反推**（Capri 2026-09-18 拍板）：
    有确定店铺时按该店商品拟合该店专属价，没锁店时用全网实测基线。
    详见 :mod:`agent.pricing`。
    """
    from agent.pricing import price_table

    return price_table(shop_id)

def _enrich_plan_fees(plan: dict) -> dict:
    """给最终方案补齐花材支数、单支价与费用结构（**两段式定价**）。

    LLM 语义路径下 design 内层花材会被 LLM 输出覆盖，这里在合并后统一重算：
    - 每种花材挂 qty / unit_price（单价取自该店价表）；
    - design.fees 费用结构；
    - budget_breakdown.items 的「主花/配材/叶材」明细与 fees 对齐。

    价表来源见 :func:`_price_table`（该店实测 → 全网基线）。
    返回新 dict（深拷贝，不改入参）。
    """
    plan = copy.deepcopy(plan)
    d = plan.get('design') or {}
    main = [m for m in d.get('main_flowers') or [] if isinstance(m, dict)]
    fillers = [f for f in d.get('fillers') or [] if isinstance(f, dict)]
    foliage = [g for g in d.get('foliage') or [] if isinstance(g, dict)]
    if not main:
        return plan
    table = _price_table(str(plan.get('shop_id') or ''))
    tier = _get_tier(plan.get('budget_num'), None)
    stems = _alloc_stems(tier, main, fillers, foliage, plan.get('budget_num'),
                         plan.get('stem_count'), table)
    for fl in main + fillers + foliage:
        fl['qty'] = stems.get(fl['name'], 1)
        fl['unit_price'] = round(table.unit_price(fl['name']), 1)
    _steps = plan.get('diy_steps') or []
    if isinstance(_steps, list):
        _new_steps: list[str] = []
        for _s in _steps:
            _t = str(_s)
            for _name, _q in stems.items():
                _t = re.sub(f'{re.escape(_name)}\\s*[×xX*]\\s*\\d+', f'{_name}×{_q}', _t)
            _new_steps.append(_t)
        plan['diy_steps'] = _new_steps
    source_label = {'override': '商家设定的标准价', 'merchant': '本店在售商品',
                    'baseline': '平台在售商品'}.get(table.source, '平台在售商品')
    d['fees'] = {
        'base_fee': table.base_fee,
        'base_standard': f'包装、手工与基础服务 {table.base_fee:.0f} 元/束（按{source_label}推算）',
        'stem_count': '、'.join(f"{f['name']}×{stems.get(f['name'], 1)}" for f in main + fillers + foliage),
        'note': f'单支价按{source_label}推算，含包装与手工；实际以门店确认为准。',
        'price_source': table.source,
        'price_samples': table.samples,
    }
    pkg_name = d.get('packaging') or '花束'
    pkg = {'name': pkg_name, 'id': 'PK_BOX' if '礼盒' in pkg_name else 'PK_BOUQUET'}
    plan['budget_breakdown'] = _build_budget_breakdown(main, fillers, foliage, pkg, tier,
                                                       plan.get('budget_num'),
                                                       plan.get('stem_count'), table)
    # 明细重算 → 顶层价格必须跟着收口。这里是 LLM 合并链路的**最后一道**重算，
    # 漏了这步就会出现「明细 547 元、卡片与下游只认 258 元」（见 _sync_plan_price）。
    _sync_plan_price(plan, tier.get('label', ''))
    return plan

def _alloc_stems(tier: dict, main: list[dict], fillers: list[dict], foliage: list[dict],
                 budget_num: int | None = None, stem_count: int | None = None,
                 table=None) -> dict[str, int]:
    """按预算 + 该店价表为每种花材分配具体支数（两段式定价的反解）。

    定价模型：``总价 = 基础费 + Σ(支数 × 单价)``（见 ``agent/pricing.py``）。
    有明确预算时反解主花支数：先扣掉基础费，再除以「主花均价 + 配材比例×配材均价」
    —— 即每支主花连同它按比例带出来的配材一共占用多少预算。

    ⚠️ 单价一律来自**该店实测价表**，不再用代码里的常数（那套高估 1.5~6.4 倍，
    会把花量算得远少于同价位现成品）。

    Args:
        tier: 预算档 dict（含 ``tier`` 与 ``range``）。
        main / fillers / foliage: 花材列表（含 ``name``）。
        budget_num: 用户预算（元）；为空时用档位区间中值兜底。
        stem_count: 用户明确指定的主花支数（优先级最高，不被预算覆盖）。
        table: 价表；为空则现取（未锁店时为全网基线）。

    Returns:
        ``{花名: 支数}``，供方案明细 / 预算 / 步骤精确引用。
    """
    if table is None:
        table = _price_table()
    tkey = tier.get('tier', 'T2')
    # 用户明确支数优先（如「11 朵」）→ 直接作为主花总数，不被预算档覆盖。
    if stem_count is not None and main:
        total_main = int(stem_count)
    else:
        total_main = _TIER_MAIN_STEMS.get(tkey, 10)
    # 用户没给预算时，用预算档区间的中值当预算 —— 比"档位直接拍一个支数"更自洽，
    # 而且会随该店实际单价浮动（同样一档，贵的店自然给得少）。
    if budget_num is None and stem_count is None and main:
        rng = list(tier.get('range') or [])
        if len(rng) == 2 and rng[1] > rng[0]:
            lo, hi = float(rng[0]), float(rng[1])
            # 上界明显是「无上限」哨兵时（如 T6 的 99999）改为取下界的 1.6 倍
            mid = lo * 1.6 if hi > lo * 4 else (lo + hi) / 2
            budget_num = max(int(mid), 60)
    # 有明确预算时按预算推算主花数量；但用户已显式给支数（stem_count）时
    # 预算仅作参考、不覆盖支数，避免「11 朵」被预算约束悄悄改少。
    if budget_num is not None and main and stem_count is None:
        main_unit = sum(table.unit_price(m['name']) for m in main) / len(main)
        side = fillers + foliage
        side_ratio = 0.4 if fillers else 0.25
        side_unit = (sum(table.unit_price(f['name']) for f in side) / len(side)) if side else 0.0
        per_main_cost = main_unit + side_ratio * side_unit
        main_budget = max(0.0, float(budget_num) - table.base_fee)
        total_main = (max(1, int(main_budget / per_main_cost))
                      if per_main_cost > 0 else _TIER_MAIN_STEMS.get(tkey, 10))
    total_main = max(1, min(int(total_main), _MAX_MAIN_STEMS))
    per_main = max(1, total_main // max(len(main), 1))
    stems: dict[str, int] = {}
    for i, f in enumerate(main):
        stems[f['name']] = per_main + (1 if i < total_main % max(len(main), 1) else 0)
    side_total = max(1, round(total_main * (0.4 if fillers else 0.25)))
    side_pool = [f['name'] for f in fillers] + [f['name'] for f in foliage]
    if side_pool:
        per_side = max(1, side_total // len(side_pool))
        for i, f in enumerate(side_pool):
            stems[f] = per_side + (1 if i < side_total % len(side_pool) else 0)
    return stems

# DIY 步骤条数上限。规则引擎固定生成 6 步；模型输出不受控，需要钳制。
_DIY_STEPS_MAX = 10


def _cap_steps(steps: Any) -> list[str]:
    """清洗并限制 DIY 步骤条数。

    Args:
        steps: 模型给出的步骤（可能是任意类型）。

    Returns:
        去掉空项后的字符串列表，最多 ``_DIY_STEPS_MAX`` 条。

    为什么需要：步骤条数完全由 LLM 生成、**不可控** —— 审计报告（2026-09-18）提到
    「制作步骤 146 步」，对用户毫无意义（没人会照做 146 步）。规则引擎固定 6 步，
    这里把模型输出对齐到同一量级。原本是直接把模型数组原样塞进方案。
    """
    if not isinstance(steps, list):
        return []
    out = [str(s).strip() for s in steps if str(s).strip()]
    if len(out) > _DIY_STEPS_MAX:
        logger.warning('[tools] DIY 步骤 %d 条超出上限 %d，已截断', len(out), _DIY_STEPS_MAX)
        out = out[:_DIY_STEPS_MAX]
    return out


def _build_diy_steps(main: list[dict], fillers: list[dict], foliage: list[dict], color_scheme: list[str], packaging: dict | None) -> list[str]:
    """生成可照做的分步插花指引（基于本方案实际花材/数量/包装）。"""
    m = [f['name'] for f in main if f]
    f1 = [f['name'] for f in fillers if f]
    f2 = [f['name'] for f in foliage if f]
    pk_name = packaging['name'] if packaging else '花束'
    pk_desc = packaging.get('description', '') if packaging else ''
    colors = '/'.join(color_scheme) or '自然色系'
    trim_m = '；'.join(f'{n}斜剪 45° 并去下半叶' for n in m) or '玫瑰斜剪 45° 并去下半叶'
    trim_f = '；'.join(f'{n}短剪保留 1/2 长度' for n in f1) or '满天星短剪成簇'
    trim_g = '；'.join(f'{n}留长 2/3 做托底勾边' for n in f2) or '尤加利留长做托底'
    # 单一花材（无配材且无叶材）：DIY 步骤不得编造满天星/尤加利等外搭花材。
    if not f1 and not f2:
        step3 = f'填充层次：本束为单一花材，不额外搭配配材与叶材；各支主花交错分布、花头朝向一致并留出空隙，即可形成饱满层次。'
        step4 = f'配色比例：单一花材按色系 {colors} 统一色调，可同色系深浅过渡，避免拼入杂色。'
        outer = '整理外轮廓'
    else:
        step3 = f'填充层次：{trim_f} 填补空隙，{trim_g} 勾边制造空气感，形成前低后高、疏密有致。'
        step4 = f'配色比例：按色系 {colors} 控制主花:配材 ≈ 7:3，避免头重脚轻或色彩打架。'
        outer = '整理外层叶材外扩'
    return [f"1. 备材处理（修剪）：{trim_m}。其中{('玫瑰需去刺' if any('玫瑰' in n for n in m) else '无刺花材无需去刺')}，{('百合需摘除雄蕊防染色' if any('百合' in n for n in m) else '无需特殊处理')}。", '2. 定高构图：以主花为视觉重心，整体高度约为花束/花器的 1.5 倍；先插主花确定骨架与朝向，各主花交错分布、花头朝向一致。', step3, step4, f'5. 包装收尾：用「{pk_name}」（{pk_desc}）螺旋扎制并{outer}，丝带/韩素纸收尾，如有贺卡随花附赠。', '6. 醒花养护：完成后深水醒花 2-4 小时再摆放，详见「养护建议」。']

def _build_care_tips(main: list[dict]) -> str:
    """生成养护建议（通用 + 针对主花的特例提示）。"""
    names = {f['name'] for f in main if f}
    tips = ['收到后斜剪根部 45°，深水醒花 2-4 小时再入瓶；', '每日换水并清洗花茎切口，花瓶水位保持 2/3；', '远离空调出风口与阳光直射，可延长花期 3-7 天。']
    if '百合' in names:
        tips.append('百合：摘除雄蕊避免花粉染色衣物，花蕊变褐及时剪去。')
    if '绣球' in names:
        tips.append('绣球：喜水，可整支浸入水中 1-2 小时急救脱水；花头可轻柔喷水。')
    if '向日葵' in names:
        tips.append('向日葵：花头重，建议浅水位并支托花茎，防止垂头。')
    return ''.join(tips)

def _build_card_message(recipient: str, occasion_phrase: str, style_label: str, tone: str, short_meaning: str) -> str:
    """生成可复用的贺卡寄语文案（场景基调优先，避免长串花语堆砌）。"""
    base = f'致{recipient}：{occasion_phrase}之际，送上这束{style_label}花束，'
    if tone:
        return base + f'愿它替我传递「{tone}」。'
    return base + f'愿它替我传递{short_meaning}。'

def _build_budget_breakdown(main: list[dict], fillers: list[dict], foliage: list[dict],
                            packaging: dict | None, tier: dict, budget_num: int | None,
                            stem_count: int | None = None, table=None) -> dict:
    """按**两段式定价**生成预算明细：``总价 = 基础费 + Σ(支数 × 单价)``。

    这是价格的最终产出点（``_sync_plan_price`` 以此为准），所以口径必须干净：
    - 单价来自该店价表（``agent/pricing``），不是代码里的常数；
    - 「包装与手工」= 基础费（实测得出），涵盖包装材料、修剪扎制与基础服务 ——
      **不再单列人工费/装饰费**，否则会把基础费里的同一笔钱算两遍。

    stem_count 为用户明确的支数（如「11 朵」），透传给 _alloc_stems，
    保证预算明细与方案主花支数一致，不被预算机械重算覆盖。
    """
    if table is None:
        table = _price_table()

    def cost(flowers: list[dict]) -> float:
        return sum(stems.get(f['name'], 1) * table.unit_price(f['name']) for f in flowers)

    stems = _alloc_stems(tier, main, fillers, foliage, budget_num, stem_count, table)
    main_cost, filler_cost, foliage_cost = cost(main), cost(fillers), cost(foliage)
    base_fee = float(table.base_fee)
    # ⚠️ 逐项取整后再求和 —— 保证「明细加起来 == 合计」（用户和店家会当场对账）。
    # 曾写成 round(总和) 而明细各自 round，实测差 1 元（792 vs 793）。
    amounts = [round(main_cost), round(filler_cost), round(foliage_cost), round(base_fee)]
    total = sum(amounts)
    source_label = {'override': '商家设定的标准价', 'merchant': '本店在售商品',
                    'baseline': '平台在售商品'}.get(table.source, '平台在售商品')
    items = [
        {'item': '主花', 'detail': '、'.join(f"{m['name']}×{stems.get(m['name'], 1)}" for m in main) or '无', 'amount': amounts[0]},
        {'item': '配材', 'detail': '、'.join(f"{f['name']}×{stems.get(f['name'], 1)}" for f in fillers) or '无', 'amount': amounts[1]},
        {'item': '叶材', 'detail': '、'.join(f"{f['name']}×{stems.get(f['name'], 1)}" for f in foliage) or '无', 'amount': amounts[2]},
        {'item': '包装与手工', 'detail': '含包装材料与修剪扎制', 'amount': amounts[3]},
    ]
    return {
        'total_estimate': total,
        'currency': 'CNY',
        'items': items,
        'fees': {
            'base_fee': base_fee,
            'base_standard': f'包装、手工与基础服务 {base_fee:.0f} 元/束（按{source_label}推算）',
            'note': f'单支花材价按{source_label}推算，已含包装与手工；下单前请以门店确认为准。',
            'price_source': table.source,
        },
        'note': f'以上按{source_label}推算，实际价格以门店/供应商为准。',
    }

def _sync_plan_price(plan: dict, tier_label: str = '') -> dict:
    """把顶层价格字段与最终的 ``budget_breakdown`` 对齐（价格的**单一真相源**）。

    为什么需要（2026-09-18 实测发现）：``price`` / ``price_text`` / ``estimated_price``
    原本只在 :func:`_build_plan` 里写一次，而 ``budget_breakdown`` 在 LLM 语义路径上
    会被**重算 4 次**（``_merge_plan`` ×3 + :func:`_enrich_plan_fees` ×1）。
    模型换过花材后，明细按新花材重算，顶层价格却还停在 baseline ——
    实测分叉到「明细合计 547 元、顶层只认 258 元」。
    这是 2026-09-18「DIY 补数值价格」修复留下的缺口：补了字段，但没管重算同步。

    ⚠️ 危害方向：``price`` 是给下游结算用的，**分叉意味着按便宜的价扣款、
    按真实的单备货**。所以任何重算明细的地方都必须回到这里收口。

    ⚠️ 单位是「元」；平台商品卡返回的 price 单位是「分」，接入方务必按字段区分。

    Args:
        plan: 方案 dict（会被就地写入 price / price_unit / price_text / estimated_price）。
        tier_label: 预算档标签；留空则按 ``budget_num`` 现算。

    Returns:
        同一个 plan 对象。
    """
    if not isinstance(plan, dict):
        return plan
    total = (plan.get('budget_breakdown') or {}).get('total_estimate')
    if total is None:
        return plan
    total = int(total)
    label = tier_label or _get_tier(plan.get('budget_num'), None).get('label', '')
    plan['price'] = total
    plan['price_unit'] = 'CNY'
    # 文案与数值**同口径**：不能出现「文案说 300、扣款 205」
    plan['price_text'] = f'约 {total} 元（{label}档）' if label else f'约 {total} 元'
    plan['estimated_price'] = plan['price_text']
    return plan


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
    if main and not fillers and not foliage:
        f0 = main_raw[0] if isinstance(main_raw[0], dict) else {}
        nm = str(f0.get('name') or main).strip()
        q = f0.get('qty')
        qtext = f'共 {int(q)} 枝' if isinstance(q, (int, float)) and int(q) > 0 else ''
        return (f'{style_label}风格纯{nm}花束，仅使用{nm}一种花材'
                f'{("（" + qtext + "）") if qtext else ""}，{pk}包装，色调统一，'
                f'背景干净柔和，摄影级静物，高级感')

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
            f'色调{colors}，{pk}包装，背景干净柔和，摄影级静物，高级感')


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
            if ld.get(key) not in (None, '', []):
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


# 「复制用料清单」文本长度上限：异常 plan 不应生成撑爆剪贴板/消息的文本。
_COPY_TEXT_MAX = 2000


def build_plan_copy_text(plan: dict) -> str:
    """把 DIY 方案渲染成一份「可直接发给店家的用料需求清单」纯文本。

    为什么需要（Capri 2026-09-18 拍板：先做 DIY 的轻量成交通口）：
    平台没有定制 SKU，方案生成完就断了 —— 用户拿不到任何**可执行**的东西，
    商家也永远看不到这个需求。在「定制需求单」接口落地（待平台方）之前，
    「复制一份清单去找店家沟通」是让 DIY 从**体验价值**变成**商业价值**的唯一出口。

    设计取舍：
    - **纯文本而非结构化**：用户要的是能直接粘进微信/客服窗口的内容，
      JSON 对用户无意义；接入方也只需一个复制按钮，不必理解内部字段。
    - **复用 ``budget_breakdown``**：明细口径与卡片**完全一致**，
      避免出现「清单一个价、卡片另一个价」。
    - **只新增字段**：结果写入 ``plan['copy_text']``，不改动任何既有字段，
      前端不渲染它也不会有副作用。

    ⚠️ 必须在本方案的缺料标注（:func:`annotate_shop_materials`）**之后**调用，
    否则 ``unavailable_materials`` 还不是最新值，清单会漏掉「该店暂无」的提醒。

    Args:
        plan: 已定型的方案 dict。

    Returns:
        纯文本清单；plan 无效时返回空字符串（调用方据此跳过「复制」入口）。
    """

    def _s(value: Any, default: str = '') -> str:
        return str(value).strip() if value not in (None, '') else default

    if not isinstance(plan, dict) or not plan:
        return ''
    design = plan.get('design') if isinstance(plan.get('design'), dict) else {}
    lines = ['【定制花束需求单】', f"方案：{_s(plan.get('name'), '定制花束')}"]

    head: list[str] = []
    if _s(plan.get('recipient')):
        head.append(f"对象：{_s(plan['recipient'])}")
    if _s(plan.get('occasion')):
        head.append(f"场合：{_s(plan['occasion'])}")
    style = _s(plan.get('substyle')) or _s(plan.get('style'))
    # 方案名通常已含风格（「韩式甜美·生日花束」）→ 不重复输出
    if style and style not in _s(plan.get('name')):
        head.append(f'风格：{style}')
    if head:
        lines.append('｜'.join(head))

    mid: list[str] = []
    colors = design.get('color_scheme') or []
    if colors:
        mid.append('配色：' + '/'.join(_s(c) for c in colors if _s(c)))
    if _s(design.get('packaging')):
        mid.append('包装：' + _s(design['packaging']))
    if mid:
        lines.append('｜'.join(mid))

    fees = design.get('fees') if isinstance(design.get('fees'), dict) else {}
    if _s(fees.get('stem_count')):
        lines.append(f"用料：{_s(fees['stem_count'])}")

    breakdown = plan.get('budget_breakdown') if isinstance(plan.get('budget_breakdown'), dict) else {}
    items = [it for it in (breakdown.get('items') or []) if isinstance(it, dict)]
    # 费用项分三类处理：
    # - 「包装与手工」（基础费）：只报项名 + 金额 —— 拼成「包装与手工：含包装材料…」
    #   是同义反复，给店家的清单要尽量干净；
    # - 旧结构的「人工费/装饰费」：合并成一行（兼容历史缓存的方案）；
    # - 其余（主花/配材/叶材）：报「项目：用料 —— 金额」。
    _FEE_ITEMS = ('人工费', '装饰费')
    _BARE_ITEMS = ('包装与手工', '基础费')
    details: list[str] = []
    fee_total = 0.0
    has_fee = False
    for it in items:
        item, detail = _s(it.get('item')), _s(it.get('detail'))
        amount = it.get('amount') if isinstance(it.get('amount'), (int, float)) else None
        if item in _FEE_ITEMS:
            has_fee = True
            fee_total += amount or 0
            continue
        if not amount:
            # 「配材：无 —— 0 元」这类空项不进清单（单一花材方案必然出现）
            continue
        seg = item if item in _BARE_ITEMS else (
            f'{item}：{detail}' if item and detail else (item or detail))
        if seg:
            details.append(f'· {seg} —— {int(round(amount))} 元')
    if details:
        lines.append('')
        lines.append('费用参考')
        lines.extend(details)
        if has_fee and fee_total:
            lines.append(f'· 人工与装饰费 —— {int(round(fee_total))} 元')
    price = plan.get('price')
    if isinstance(price, (int, float)) and price:
        lines.append(f'合计约 {int(price)} 元（按平台在售价估算，实际以门店报价为准）')
    elif _s(breakdown.get('note')):
        lines.append(_s(breakdown['note']))

    missing = [m for m in (plan.get('unavailable_materials') or []) if _s(m)]
    if missing:
        lines.append('')
        lines.append('注：以下原料这家店当前暂无，可能需另外采购 —— ' + '、'.join(_s(m) for m in missing))

    lines.append('')
    lines.append('（本清单由 AI 花艺小助手按平台在售价生成，实际报价以门店为准）')
    text = '\n'.join(lines).strip()
    if len(text) > _COPY_TEXT_MAX:
        # 截断必须留痕：标明「还有下文」，避免用户以为清单到此为止。
        logger.warning('[design] copy_text 超长（%d 字符）已截断', len(text))
        text = text[:_COPY_TEXT_MAX].rstrip() + '…（清单较长，已截断）'
    return text


def design_diy_plan(requirements: str, shop_id: str = '', session_requirement: FlowerRequirement | None = None) -> dict:
    """设计一份结构化 DIY 花艺方案（RAG + LLM 语义生成，规则引擎兜底）。

    链路：RAG 检索知识库 → DeepSeek 生成语义化方案 → 规则引擎 _build_plan 补全结构/兜底。
    返回可供 UI 渲染、生图与下单承接的结构化 dict。
    shop_id 非空时，方案会带上「哪些原料该店暂时没有」的标注（见
    :func:`annotate_shop_materials`）——**保留花材本身，只标注，不自动替换**。
    """
    plan = design_with_llm(requirements, shop_id=shop_id, session_requirement=session_requirement)
    from agent.plan_validator import annotate_plan_validation
    plan = annotate_plan_validation(plan, session_requirement)
    plan = annotate_shop_materials(plan, shop_id)
    # 「复制用料清单」：必须在缺料标注之后生成，否则清单漏掉「该店暂无」提醒
    if isinstance(plan, dict):
        plan['copy_text'] = build_plan_copy_text(plan)
    return plan

def revise_with_llm(plan: str, feedback: str, shop_id: str = '') -> dict:
    """语义化改版：RAG 检索 + DeepSeek 基于已有方案与反馈调整，规则引擎兜底。

    反馈里明确要改的（预算/风格/色系/移除花材）必须落实；未提及维度保持原方案。
    shop_id 非空（或从原方案继承）时，改版后的原料仍限定在该店铺在售范围内。
    """
    original = _parse_plan(plan)
    # 店铺锁定沿用原方案，避免改版后跳出该店在售范围。
    shop_id = shop_id or str(original.get('shop_id') or '')
    dims = _dims_from_plan(original)
    fb = _extract_feedback(feedback)
    # 排除项**跨轮继承**：用户上一轮说过「不要满天星」，这一轮只说「换个配色」时不能失效
    # （`_build_plan` 会把它持久化到 `plan['exclude_flowers']`，这里并集回来）。
    exclude = set(fb['exclude']) | {str(n) for n in (original.get('exclude_flowers') or [])}
    dims.update(fb['dims'])
    baseline = _build_plan(dims, version=original.get('version', 1) + 1, parent_id=original.get('plan_id'), exclude_flowers=exclude, shop_id=shop_id)
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
        new_plan = _merge_plan(baseline, llm_plan, exclude_flowers=exclude)
        new_plan['plan_id'] = baseline['plan_id']
        new_plan['version'] = original.get('version', 1) + 1
        new_plan['parent_id'] = original.get('plan_id')
        new_plan['diy'] = True
        if shop_id:
            new_plan['shop_id'] = shop_id
        from agent.plan_validator import annotate_plan_validation
        return annotate_plan_validation(new_plan)
    except Exception:
        logger.exception('[revise] LLM 语义改版失败，回退规则引擎')
        from agent.plan_validator import annotate_plan_validation
        return annotate_plan_validation(baseline)

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
