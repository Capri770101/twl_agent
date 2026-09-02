"""tools.py —— 内建工具实现。

设计：
- 每个工具用 @register_tool 装饰，自动写入 TOOL_REGISTRY（名称 / 中文描述 / 参数 JSON Schema / 实现）。
- agent 从注册表自动生成「工具说明书」注入 system prompt，并生成 OpenAI function-calling 定义。
- 新增工具只要写一个带装饰器的函数，agent 与提示词零改动。
- 需要用户上下文（如 user_id）的工具加 inject_context=True，execute_tool 时注入 _context。
"""
from __future__ import annotations

import copy
import inspect
import json
import logging
import re
import uuid
from typing import Any

from agent.engine.llm import call_llm
from agent.engine.ui_protocol import UIType
from agent.knowledge import get_by_id, query_knowledge
from domain.requirements import FlowerRequirement
from backend.storage import memory, tasks
from backend.storage.repository import repo
from backend.data_gateway import auto_create_order, auto_list_orders, auto_query, auto_search_plans, auto_search_shops, describe_table, discover_database, get_file_info, infer_mapping, inspect_source, list_files, list_tables, query_readonly, read_file, sample_rows, search_files, tool_result
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

def _rank_plans(plans: list[dict], req: FlowerRequirement | None) -> list[dict]:
    """相关性排序并截断：命中预算/色系/对象者优先，最多返回 3 款。

    保证「推荐的产品符合需求描述」，且卡片不超过 3 个。
    """

    def score(p: dict) -> int:
        s = 0
        if not req:
            return s
        if req.budget_min is not None:
            lo, hi = (req.budget_min, req.budget_max or req.budget_min)
            if lo <= p.get('price', 0) <= hi * 1.5:
                s += 10
        if req.colors:
            blob = ((p.get('name') or '') + (p.get('desc') or '') + ' '.join(p.get('tags') or [])).lower()
            if any(c.lower() in blob for c in req.colors):
                s += 5
        if req.recipient:
            blob = (p.get('name') or '') + (p.get('desc') or '')
            if req.recipient.lower() in blob.lower():
                s += 3
        return s
    return sorted(plans, key=score, reverse=True)[:3]

async def _store_diy_plan(plan: dict, _context: dict | None) -> None:
    """把最新 DIY 方案写入当前会话（会话级，替代旧全局变量，杜绝多用户串号）。

    latest_diy_plan 供生图生成精确 prompt；selected_plan 作为「最近引用方案」，
    让 search_shops / create_order 的 latest 占位符解析到正确方案。
    """
    uid = (_context or {}).get('user_id', '')
    sid = (_context or {}).get('session_id', '')
    if not uid or not sid:
        return
    await memory.set_session_json(uid, sid, 'latest_diy_plan', plan)
    await memory.set_session_json(uid, sid, 'selected_plan', plan)

async def _resolve_session_plan(plan: str | None, _context: dict | None) -> dict | None:
    """把工具参数里的方案引用解析为具体方案 dict。

    - "latest" / "latest_diy" / 空：会话「最近引用方案」→ 会话最新 DIY 方案 → 首条预设方案。
    - 显式 plan_id：先查仓库（现有方案）；查不到且形如 DIY_xxx 时回退到会话最新 DIY 方案。
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
        plans = await repo.search_plans('')
        return plans[0] if plans else None
    found = await repo.get_plan(plan)
    if found:
        return found
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

@register_tool(name='search_plans', description='搜索商家预设花卉方案（含名称、价格、描述、效果图 URL）；会结合当前会话的结构化需求（预算/色系/风格）做软过滤。', parameters={'type': 'object', 'properties': {'keyword': {'type': 'string', 'description': '搜索关键词，如 康乃馨 / 玫瑰 / 母亲；留空则浏览全部'}}, 'required': ['keyword']}, inject_context=True, tags=['plan'])
async def search_plans(keyword: str, _context: dict | None=None) -> str:
    """搜索商家预设方案（按关键词搜索；有定位时限定配送范围内店铺的方案）。

    LLM 直接传入关键词（如「玫瑰」「母亲 生日」），不需要预提取需求。
    命中结果按相关性排序并截断到 3 款。
    当 context 含 shop_id 时，限定该店铺的方案。
    """
    req = _requirement_from_context(_context)
    location = None
    if _context:
        location = _context.get('location') or (req.location if req else None)
    locked_shop = (_context or {}).get('shop_id')
    if locked_shop:
        plans = await repo.search_plans(keyword, requirement=req, location=None)
        plans = [p for p in plans if p.get('shop_id') == locked_shop]
    else:
        plans = await repo.search_plans(keyword, requirement=req, location=location)
    uid = (_context or {}).get('user_id', '')
    sid = (_context or {}).get('session_id', '')
    diy_hits: list[dict] = []
    if uid:
        try:
            from backend.storage.diy import search_diy_plans as _search_diy
            diy_hits = await _search_diy(uid, req)
        except Exception:
            logger.exception('[tools] 个人 DIY 方案检索失败')
    if diy_hits:
        combined = _rank_plans(diy_hits, req) + _rank_plans(plans, req)
        result = combined[:3]
    else:
        result = _rank_plans(plans, req)
    if result and sid:
        await memory.set_session_json(uid, sid, 'selected_plan', result[0])
    return json.dumps(result, ensure_ascii=False)

@register_tool(name='get_plan_detail', description='根据方案 ID 获取单个方案的完整详情。', parameters={'type': 'object', 'properties': {'plan_id': {'type': 'string', 'description': '方案 ID，如 P001'}}, 'required': ['plan_id']}, tags=['plan'])
async def get_plan_detail(plan_id: str) -> str:
    """获取方案详情（DIY_ 前缀回落 diy_plans 资产库）。"""
    plan = await repo.get_plan(plan_id)
    if not plan and str(plan_id).startswith('DIY_'):
        try:
            from backend.storage.diy import get_diy_plan
            plan = await get_diy_plan(plan_id)
        except Exception:
            plan = None
    return json.dumps(plan or {'error': 'not found'}, ensure_ascii=False)

@register_tool(name='retrieve_knowledge', description='检索花卉 DIY 知识库：花材(花语/色系/季节/价格档/搭配性)、风格体系、搭配规则、预算映射、包装器型、商家智库（店铺的风格/擅长场景/价位/服务/卖点）。在设计方案前调用以获取可靠的领域知识，避免凭空编造；找店铺时用 shop 域。', parameters={'type': 'object', 'properties': {'domain': {'type': 'string', 'description': '检索域：flower(花材) | style(风格) | pairing(搭配规则) | budget(预算) | packaging(包装) | shop(商家智库) | scene(场景) | proven(用户验证过的实战方案) | all(全部)'}, 'query': {'type': 'string', 'description': '关键词或自然语言，如 母亲/生日/北欧/200元/能做婚礼布置的店'}}, 'required': ['domain', 'query']}, tags=['knowledge'])
def retrieve_knowledge(domain: str, query: str) -> str:
    """检索知识库，返回相关条目 JSON。"""
    return json.dumps(query_knowledge(domain, query), ensure_ascii=False)

@register_tool(name='fs_list', description='列出当前项目允许范围内的文件/目录。', parameters={'type': 'object', 'properties': {'path': {'type': 'string', 'description': '起始路径，默认 .'}, 'exclude': {'type': 'array', 'items': {'type': 'string'}, 'description': '排除的目录名或片段'}, 'depth': {'type': 'integer', 'description': '递归深度'}}, 'required': []}, tags=['filesystem'])
def fs_list(path: str='.', exclude: list[str] | None=None, depth: int=2) -> str:
    try:
        return tool_result(True, list_files(path=path, exclude=exclude, depth=depth))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='fs_read', description='读取当前项目允许范围内的文本文件。', parameters={'type': 'object', 'properties': {'path': {'type': 'string', 'description': '文件路径'}, 'max_length': {'type': 'integer', 'description': '最多读取字符数'}}, 'required': ['path']}, tags=['filesystem'])
def fs_read(path: str, max_length: int=200000) -> str:
    try:
        return tool_result(True, read_file(path=path, max_length=max_length))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='fs_search', description='在当前项目允许范围内按 glob 模式搜索文件。', parameters={'type': 'object', 'properties': {'pattern': {'type': 'string', 'description': 'glob 模式，如 **/*.sql'}, 'path': {'type': 'string', 'description': '搜索根目录'}}, 'required': ['pattern']}, tags=['filesystem'])
def fs_search(pattern: str, path: str='.') -> str:
    try:
        return tool_result(True, search_files(pattern=pattern, path=path))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='fs_info', description='查看当前项目允许范围内文件或目录的元信息。', parameters={'type': 'object', 'properties': {'path': {'type': 'string', 'description': '文件或目录路径'}}, 'required': ['path']}, tags=['filesystem'])
def fs_info(path: str) -> str:
    try:
        return tool_result(True, get_file_info(path=path))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_list_tables', description='列出当前数据库中的所有表。', parameters={'type': 'object', 'properties': {}, 'required': []}, tags=['database'])
def db_list_tables() -> str:
    try:
        return tool_result(True, list_tables())
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_describe_table', description='查看数据库表结构。', parameters={'type': 'object', 'properties': {'table': {'type': 'string', 'description': '表名'}}, 'required': ['table']}, tags=['database'])
def db_describe_table(table: str) -> str:
    try:
        return tool_result(True, describe_table(table))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_sample_rows', description='抽样读取数据库表中的少量数据。', parameters={'type': 'object', 'properties': {'table': {'type': 'string', 'description': '表名'}, 'limit': {'type': 'integer', 'description': '样本行数'}}, 'required': ['table']}, tags=['database'])
def db_sample_rows(table: str, limit: int=5) -> str:
    try:
        return tool_result(True, sample_rows(table=table, limit=limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_query_readonly', description='只读执行 SELECT SQL 查询，禁止写操作。', parameters={'type': 'object', 'properties': {'sql': {'type': 'string', 'description': 'SELECT 语句'}, 'max_rows': {'type': 'integer', 'description': '最大返回行数'}}, 'required': ['sql']}, tags=['database'])
def db_query_readonly(sql: str, max_rows: int=100) -> str:
    try:
        return tool_result(True, query_readonly(sql=sql, max_rows=max_rows))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_discover', description='自动发现当前数据库的表结构与样本数据。', parameters={'type': 'object', 'properties': {'sample_limit': {'type': 'integer', 'description': '每张表的样本行数（默认 1，传 0 只返回结构）'}}, 'required': []}, tags=['database'])
def db_discover(sample_limit: int=1) -> str:
    try:
        return tool_result(True, discover_database(sample_limit=sample_limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='source_inspect', description='一次性概览当前项目文件与数据库表结构，适合刚接入时自动发现数据源。', parameters={'type': 'object', 'properties': {'include_db': {'type': 'boolean', 'description': '是否包含数据库结构'}, 'include_files': {'type': 'boolean', 'description': '是否包含文件列表'}, 'depth': {'type': 'integer', 'description': '文件目录递归深度'}}, 'required': []}, tags=['discovery'])
def source_inspect(include_db: bool=True, include_files: bool=True, depth: int=1) -> str:
    try:
        return tool_result(True, inspect_source(include_db=include_db, include_files=include_files, depth=depth))
    except Exception as exc:
        return tool_result(False, error=str(exc))

_RECIPIENT_KW = {'妈妈': '母亲', '母亲': '母亲', '妈': '母亲', '娘': '母亲', '恋人': '恋人', '女朋友': '恋人', '男朋友': '恋人', '老婆': '恋人', '老公': '恋人', '对象': '恋人', '爱人': '恋人', '男友': '恋人', '女友': '恋人', '先生': '恋人', '丈夫': '恋人', '朋友': '朋友', '闺蜜': '朋友', '兄弟': '朋友', '同事': '朋友', '姐妹': '朋友', '自己': '自己', '悦己': '自己', '我': '自己', '长辈': '长辈', '老人': '长辈', '父母': '长辈', '领导': '长辈', '上司': '长辈', '老板': '长辈', '老师': '长辈', '宝宝': '宝宝', '婴儿': '宝宝', '新生儿': '宝宝'}
_OCCASION_KW = {'生日': '生日', '庆祝': '生日', '母亲节': '母亲', '父亲节': '父亲', '节': '节日', '告白': '告白', '表白': '告白', '纪念日': '告白', '求婚': '告白', '婚礼': '婚礼', '结婚': '婚礼', '领证': '婚礼', '探病': '探病', '生病': '探病', '康复': '探病', '住院': '探病', '道歉': '道歉', '对不起': '道歉', '抱歉': '道歉', '毕业': '毕业', '乔迁': '乔迁', '开业': '开业', '升职': '升职', '入职': '入职'}
_STYLE_KW = {'韩式': 'S_KOREAN', '韩系': 'S_KOREAN', '北欧': 'S_NORDIC', '简约': 'S_NORDIC', '极简': 'S_NORDIC', '复古': 'S_VINTAGE', '古典': 'S_VINTAGE', '港风': 'S_VINTAGE', '中古': 'S_VINTAGE', '自然': 'S_NATURAL', '野趣': 'S_NATURAL', '田园': 'S_NATURAL', 'ins': 'S_INS', 'ins风': 'S_INS', '网红': 'S_INS', '奶油风': 'S_INS', '法式': 'S_INS', '日式': 'S_JAPANESE', '禅': 'S_JAPANESE', '日系': 'S_JAPANESE'}
_COLOR_KW = {'红': '红', '粉': '粉', '白': '白', '香槟': '香槟', '紫': '紫', '蓝': '蓝', '黄': '黄', '橙': '橙', '绿': '绿', '多彩': '多彩混合', '亮': '亮', '鲜艳': '亮', '缤纷': '多彩混合', '粉嫩': '粉', '浅粉': '粉', '桃红': '粉', '正红': '红', '酒红': '红', '橘': '橙', '鹅黄': '黄', '天蓝': '蓝', '湖蓝': '蓝', '香槟色': '香槟', '五彩': '多彩混合', '撞色': '多彩混合'}
_MOOD_KW = {'温柔': '温柔', '温馨': '温馨', '浪漫': '浪漫', '清新': '清新', '热烈': '热烈', '活泼': '活泼', '高级': '高级', '素雅': '素雅', '优雅': '优雅', '莫兰迪': '素雅', '马卡龙': '清新', '小清新': '清新', '轻奢': '高级', '低调': '素雅', '治愈': '治愈', '安静': '素雅', '甜美': '甜美', '氛围感': '优雅', '高级感': '高级'}
_BUDGET_ORAL = {'一两百': 150, '一二百': 150, '小几百': 200, '两三百': 250, '二三百': 250, '三四百': 350, '三五百': 400, '五六百': 550, '七八百': 750, '千把块': 1000, '一千': 1000, '一两千': 1500, '两三千': 2500}

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
    """
    anchor: str | None = None
    for oral, num in _BUDGET_ORAL.items():
        if oral in text:
            text = text.replace(oral, f' {num} ')
            anchor = oral
            break
    m = re.search('(\\d{2,5})\\s*(?:元|块|块钱)?', text)
    if not m:
        return (anchor, None, None, None)
    num = float(m.group(1))
    return (anchor, num, round(num * 0.8), round(num * 1.2))

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
    if req.recipient:
        req.relationship = _RELATIONSHIP_MAP.get(req.recipient)
    return req

def _extract(text: str) -> dict[str, str]:
    """从自然语言需求中抽取维度（兼容旧形态，供 DIY 设计管线 / _extract_dims 测试）。"""
    return extract_requirement(text).to_legacy_dict()

def _resolve_flowers(dims: dict[str, str], style: dict, budget_tier: dict, prefer_flowers: list[str] | None=None, exclude_flowers: set[str] | None=None) -> tuple[list[dict], list[dict], list[dict]]:
    """根据维度 + 风格 + 预算，从知识库挑主花/配材/叶材。

    Args:
        prefer_flowers: 场景模板指定的优先主花（最高权重）。
        exclude_flowers: 用户反馈中要求移除的花材名集合（迭代时用到）。
    """
    prefer_flowers = prefer_flowers or []
    exclude_flowers = exclude_flowers or set()
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
    fillers = [f for f in candidates if f.get('category') == '填充' and f['name'] not in exclude_flowers][:1] or [all_flowers.get('满天星')]
    foliage = [f for f in candidates if f.get('category') == '叶材' and f['name'] not in exclude_flowers][:1] or [all_flowers.get('尤加利')]
    return ([f for f in main if f], [f for f in fillers if f], [f for f in foliage if f])
_PRICE_UNIT = {'低': 12, '中': 28, '高': 60}
_TIER_MAIN_STEMS = {'T1': 6, 'T2': 10, 'T3': 16}
_LABOR_FEE = {'T1': 15, 'T2': 25, 'T3': 40}
_DECOR_FEE = {'T1': 10, 'T2': 18, 'T3': 30}

def _known_flower(name: str) -> dict:
    """按花名查知识库花卉（含别名匹配），查不到返回空 dict。"""
    if not name:
        return {}
    all_fl = query_knowledge('flower', '')['results']
    for f in all_fl:
        if f['name'] == name or name in f.get('aliases', []):
            return f
    return {}

def _price_tier_of(name: str) -> str:
    """花材价格档（低/中/高），知识库无记录时按「中」兜底。"""
    return _known_flower(name).get('price_tier', '中') or '中'

def _enrich_plan_fees(plan: dict) -> dict:
    """给最终方案补齐花材支数 + 人工费/装饰费（按预算档标准）。

    LLM 语义路径下 design 内层花材会被 LLM 输出覆盖，这里在合并后统一重算：
    - 每种花材挂 qty / unit_price；
    - design.fees 费用结构（含收取标准）；
    - budget_breakdown.items 的「主花/配材/叶材」明细与 fees 对齐。
    返回新 dict（深拷贝，不改入参）。
    """
    plan = copy.deepcopy(plan)
    d = plan.get('design') or {}
    main = [m for m in d.get('main_flowers') or [] if isinstance(m, dict)]
    fillers = [f for f in d.get('fillers') or [] if isinstance(f, dict)]
    foliage = [g for g in d.get('foliage') or [] if isinstance(g, dict)]
    if not main:
        return plan
    tier = _get_tier(plan.get('budget_num'), None)
    stems = _alloc_stems(tier, main, fillers, foliage, plan.get('budget_num'))
    tkey = tier.get('tier', 'T2')
    labor_fee = _LABOR_FEE.get(tkey, 25)
    decor_fee = _DECOR_FEE.get(tkey, 18)
    for fl in main + fillers + foliage:
        fl['qty'] = stems.get(fl['name'], 1)
        fl['unit_price'] = _PRICE_UNIT.get(_price_tier_of(fl['name']), 28)
    _steps = plan.get('diy_steps') or []
    if isinstance(_steps, list):
        _new_steps: list[str] = []
        for _s in _steps:
            _t = str(_s)
            for _name, _q in stems.items():
                _t = re.sub(f'{re.escape(_name)}\\s*[×xX*]\\s*\\d+', f'{_name}×{_q}', _t)
            _new_steps.append(_t)
        plan['diy_steps'] = _new_steps
    d['fees'] = {'labor_fee': labor_fee, 'labor_standard': f'人工费 {labor_fee} 元/束（含修剪、去刺、扎制、定型，按预算档标准收取）', 'decor_fee': decor_fee, 'decor_standard': f'装饰费 {decor_fee} 元/束（含丝带、贺卡、点缀饰材，按预算档标准收取）', 'stem_count': '、'.join(f"{f['name']}×{stems.get(f['name'], 1)}" for f in main + fillers + foliage), 'note': '花材按支数计费，人工费与装饰费为门店统一收取标准，下单前以门店确认为准。'}
    pkg_name = d.get('packaging') or '花束'
    pkg = {'name': pkg_name, 'id': 'PK_BOX' if '礼盒' in pkg_name else 'PK_BOUQUET'}
    plan['budget_breakdown'] = _build_budget_breakdown(main, fillers, foliage, pkg, tier, plan.get('budget_num'))
    return plan

def _alloc_stems(tier: dict, main: list[dict], fillers: list[dict], foliage: list[dict], budget_num: int | None=None) -> dict[str, int]:
    """按预算档 + 花材角色为每种花材分配具体支数。

    - 主花支数取预算档基准 _TIER_MAIN_STEMS，平均分给每种主花（不足 1 支补 1）；
    - 有明确预算时，主花数量受预算约束：预留配材/叶材/包装/人工/装饰费用后，
      剩余预算除以主花均价，防止总价远超用户预算；
    - 填充 / 叶材按主花总数的 30%-50% 配比，平均分摊。
    返回 {花名: 支数}，供方案明细 / 预算 / 步骤精确引用。
    """
    tkey = tier.get('tier', 'T2')
    total_main = _TIER_MAIN_STEMS.get(tkey, 10)
    if budget_num is not None and main:
        labor = _LABOR_FEE.get(tkey, 25)
        decor = _DECOR_FEE.get(tkey, 18)
        pkg = 35 if tier.get('tier') == 'T3' else 8
        reserved = labor + decor + pkg
        avg_unit = sum(_PRICE_UNIT.get(_price_tier_of(m['name']), 28) for m in main) / len(main)
        side_unit = sum(_PRICE_UNIT.get(_price_tier_of(f['name']), 28) for f in fillers + foliage) / max(len(fillers) + len(foliage), 1) if fillers + foliage else 28
        reserved += side_unit * 2
        main_budget = max(0, budget_num - reserved)
        total_main = max(1, int(main_budget // avg_unit))
        total_main = min(total_main, _TIER_MAIN_STEMS.get(tkey, 10) + 2)
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
    return [f"1. 备材处理（修剪）：{trim_m}。其中{('玫瑰需去刺' if any('玫瑰' in n for n in m) else '无刺花材无需去刺')}，{('百合需摘除雄蕊防染色' if any('百合' in n for n in m) else '无需特殊处理')}。", '2. 定高构图：以主花为视觉重心，整体高度约为花束/花器的 1.5 倍；先插主花确定骨架与朝向，各主花交错分布、花头朝向一致。', f'3. 填充层次：{trim_f} 填补空隙，{trim_g} 勾边制造空气感，形成前低后高、疏密有致。', f'4. 配色比例：按色系 {colors} 控制主花:配材 ≈ 7:3，避免头重脚轻或色彩打架。', f'5. 包装收尾：用「{pk_name}」（{pk_desc}）螺旋扎制并整理外层叶材外扩，丝带/韩素纸收尾，如有贺卡随花附赠。', '6. 醒花养护：完成后深水醒花 2-4 小时再摆放，详见「养护建议」。']

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

def _build_budget_breakdown(main: list[dict], fillers: list[dict], foliage: list[dict], packaging: dict | None, tier: dict, budget_num: int | None) -> dict:
    """按花材档位估算预算分项（含每种花材支数 + 人工费/装饰费收取标准）。"""

    def unit(name: str) -> int:
        return _PRICE_UNIT.get(_price_tier_of(name), 28)
    tkey = tier.get('tier', 'T2')
    stems = _alloc_stems(tier, main, fillers, foliage, budget_num)
    main_cost = sum(stems.get(m['name'], 1) * unit(m['name']) for m in main)
    filler_cost = sum(stems.get(f['name'], 1) * unit(f['name']) for f in fillers)
    foliage_cost = sum(stems.get(f['name'], 1) * unit(f['name']) for f in foliage)
    pkg_material = 35 if packaging and packaging.get('id') == 'PK_BOX' else 8
    labor_fee = _LABOR_FEE.get(tkey, 25)
    decor_fee = _DECOR_FEE.get(tkey, 18)
    total = round(main_cost + filler_cost + foliage_cost + pkg_material + labor_fee + decor_fee)
    items = [{'item': '主花', 'detail': '、'.join(f"{m['name']}×{stems.get(m['name'], 1)}" for m in main) or '玫瑰', 'amount': round(main_cost)}, {'item': '配材', 'detail': '、'.join(f"{f['name']}×{stems.get(f['name'], 1)}" for f in fillers) or '满天星', 'amount': round(filler_cost)}, {'item': '叶材', 'detail': '、'.join(f"{f['name']}×{stems.get(f['name'], 1)}" for f in foliage) or '尤加利', 'amount': round(foliage_cost)}, {'item': '包装材料', 'detail': packaging['name'] if packaging else '花束', 'amount': pkg_material}, {'item': '装饰费', 'detail': f'含丝带/贺卡/点缀（{decor_fee} 元/束，按预算档标准）', 'amount': decor_fee}, {'item': '人工费', 'detail': f'含修剪、去刺、扎制、定型（{labor_fee} 元/束，按预算档标准）', 'amount': labor_fee}]
    return {'total_estimate': total, 'currency': 'CNY', 'items': items, 'fees': {'labor': labor_fee, 'labor_standard': '按预算档收取：入门档 15 元 / 精致档 25 元 / 高级档 40 元（含修剪、去刺、扎制、定型）', 'decor': decor_fee, 'decor_standard': '按预算档收取：入门档 10 元 / 精致档 18 元 / 高级档 30 元（含丝带、贺卡、点缀饰材）', 'note': '花材费用按支数计，人工与装饰费为门店统一标准，下单前请以门店确认为准。'}, 'note': '以上为按花材档位做的估算，实际价格以门店/供应商为准。'}

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

def _build_plan(dims: dict[str, str], version: int=1, parent_id: str | None=None, exclude_flowers: set[str] | None=None) -> dict:
    """设计核心：基于维度组装一份结构化 DIY 方案（场景感知 + 细分风格）。

    Args:
        dims: 抽取出的维度（recipient/occasion/style/substyle/scene/color/mood/budget/_keep_main）。
        version: 方案版本号，迭代时递增。
        parent_id: 上一版方案 id，便于追溯。
        exclude_flowers: 反馈中要求移除的花材名集合。
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
    prefer = list(scene.get('main_flower_preference', [])) if scene else []
    keep_main = [n for n in dims.get('_keep_main', '').split(',') if n] if dims.get('_keep_main') else []
    main, fillers, foliage = _resolve_flowers(dims, style, tier, prefer_flowers=prefer or keep_main, exclude_flowers=exclude_flowers)
    main_flowers = [{'name': f['name'], 'role': '主花', 'flower_language': f.get('flower_language', [])} for f in main]
    filler_flowers = [{'name': f['name'], 'role': '填充'} for f in fillers]
    foliage_flowers = [{'name': f['name'], 'role': '叶材'} for f in foliage]
    stems = _alloc_stems(tier, main, fillers, foliage, budget_num)
    for fl in main_flowers + filler_flowers + foliage_flowers:
        fl['qty'] = stems.get(fl['name'], 1)
        fl['unit_price'] = _PRICE_UNIT.get(_price_tier_of(fl['name']), 28)
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
    effect_prompt = f"{style_label}风格花束，主花为{'、'.join(f['name'] for f in main) or '玫瑰'}，搭配{'、'.join(f['name'] for f in fillers) or '满天星'}与{'、'.join(f['name'] for f in foliage) or '尤加利'}，色调{'/'.join(color_scheme)}，{(packaging['name'] if packaging else '花束')}包装，背景干净柔和，摄影级静物，高级感"
    occ_label = dims.get('occasion') or (scene['name'] if scene else '定制')
    notes = []
    if scene:
        notes.append(f"场景模板：{scene['name']} —— {scene.get('notes', '')}")
    notes.append(f"风格：{style_label}（{style.get('description', '')}）")
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
    _stems = _alloc_stems(tier, main, fillers, foliage, budget_num)
    _flower_qty_text = '、'.join(f"{f['name']}×{_stems.get(f['name'], 1)}" for f in main + fillers + foliage if f) or '玫瑰×10'
    tkey = tier.get('tier', 'T2')
    labor_fee = _LABOR_FEE.get(tkey, 25)
    decor_fee = _DECOR_FEE.get(tkey, 18)
    plan = {'plan_id': 'DIY_' + uuid.uuid4().hex[:6], 'version': version, 'parent_id': parent_id, 'name': f'{style_label}·{occ_label}花束', 'diy': True, 'style': style_label, 'style_id': style_id, 'substyle_id': substyle_id, 'substyle': style.get('name') if substyle_id and resolved is not parent else None, 'recipient': dims.get('recipient', '通用'), 'occasion': occ_label, 'scene_id': scene['id'] if scene else None, 'scene': scene['name'] if scene else None, 'budget_num': budget_num, 'budget_tier': tier['label'], 'design': {'main_flowers': main_flowers, 'fillers': filler_flowers, 'foliage': foliage_flowers, 'color_scheme': color_scheme, 'packaging': packaging['name'] if packaging else '花束', 'meaning': meaning, 'notes': notes, 'difficulty': difficulty, 'est_time': est_time, 'shelf_life': shelf_life, 'suitable_for': suitable_for, 'caution': caution, 'mood_tags': mood_tags, 'fees': {'labor_fee': labor_fee, 'labor_standard': f'人工费 {labor_fee} 元/束（含修剪、去刺、扎制、定型，按预算档标准收取）', 'decor_fee': decor_fee, 'decor_standard': f'装饰费 {decor_fee} 元/束（含丝带、贺卡、点缀饰材，按预算档标准收取）', 'stem_count': _flower_qty_text, 'note': '花材按支数计费，人工费与装饰费为门店统一收取标准，下单前以门店确认为准。'}}, 'estimated_price': est, 'effect_prompt': effect_prompt, 'desc': f"为你设计了一份{style_label}{occ_label}花束：花材共 {_flower_qty_text}，色调{'/'.join(color_scheme)}，寓意{meaning}。含人工费 {labor_fee} 元 + 装饰费 {decor_fee} 元，预算{est}。", 'diy_steps': _build_diy_steps(main, fillers, foliage, color_scheme, packaging), 'care_tips': _build_care_tips(main), 'card_message': _build_card_message(dims.get('recipient', '朋友'), scene['name'] if scene else occ_label, style_label, tone, short_meaning), 'budget_breakdown': _build_budget_breakdown(main, fillers, foliage, packaging, tier, budget_num)}
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

def _merge_plan(baseline: dict, llm_plan: dict) -> dict:
    """用 LLM 生成的语义字段覆盖 baseline；缺字段回落 baseline，保证 schema 完整不崩。

    baseline 由规则引擎 _build_plan 产出（机械字段齐全、花材真实），LLM 负责提升语义
    贴合度（选花/配色/文案/寓意）。两者合并既治本（理解模糊需求）又稳（结构永不错位）。
    """
    plan = copy.deepcopy(baseline)
    if not isinstance(llm_plan, dict):
        return plan
    for key in ('name', 'style', 'recipient', 'occasion', 'scene', 'desc', 'effect_prompt', 'estimated_price', 'budget_tier'):
        if llm_plan.get(key) not in (None, '', []):
            plan[key] = llm_plan[key]
    ld = llm_plan.get('design')
    if isinstance(ld, dict):
        bd = plan.setdefault('design', {})
        for key in ('main_flowers', 'fillers', 'foliage', 'color_scheme', 'packaging', 'meaning', 'notes', 'diy_steps', 'care_tips', 'card_message', 'budget_breakdown', 'difficulty', 'est_time', 'shelf_life', 'suitable_for', 'caution', 'mood_tags'):
            if ld.get(key) not in (None, '', []):
                bd[key] = ld[key]
    if isinstance(ld, dict):
        real_main = [{'name': m['name']} for m in ld.get('main_flowers', []) if isinstance(m, dict) and m.get('name')]
        real_fill = [{'name': f['name']} for f in ld.get('fillers', []) if isinstance(f, dict) and f.get('name')]
        real_foli = [{'name': g['name']} for g in ld.get('foliage', []) if isinstance(g, dict) and g.get('name')]
        if real_main:
            pk_name = ld.get('packaging') or plan.get('design', {}).get('packaging') or '花束'
            pkg = {'name': pk_name, 'id': 'PK_BOX' if '礼盒' in pk_name else 'PK_BOUQUET'}
            if ld.get('diy_steps') not in (None, '', []):
                plan['diy_steps'] = ld['diy_steps']
            else:
                plan['diy_steps'] = _build_diy_steps(real_main, real_fill, real_foli, ld.get('color_scheme') or [], pkg)
            if ld.get('budget_breakdown') not in (None, '', []):
                plan['budget_breakdown'] = ld['budget_breakdown']
            else:
                tier = _get_tier(plan.get('budget_num'), None)
                plan['budget_breakdown'] = _build_budget_breakdown(real_main, real_fill, real_foli, pkg, tier, plan.get('budget_num'))
        if ld.get('card_message') not in (None, '', []):
            plan['card_message'] = ld['card_message']
    _anchor_style(plan)
    plan = _enrich_plan_fees(plan)
    return plan

def design_with_llm(requirements: str) -> dict:
    """语义化设计：RAG 检索知识库 + DeepSeek 生成方案，规则引擎作兜底与结构补全。

    相对纯规则引擎，LLM 能理解「治愈系」「有故事感」「不按常理」等模糊/语义化需求，
    从知识库召回的真实花材中组织出更贴合的方案文案、选花与配色，而非套模板。
    """
    baseline = _build_plan(_extract(requirements))
    try:
        knowledge = _retrieve_for_design(requirements)
        system = '你是资深花艺设计师。依据用户需求与下方【知识库召回】设计一份花艺方案，只输出 JSON、不要额外解释。字段须严格为：{"name":方案名,"style":风格标签,"recipient":收礼人,"occasion":场景或节日,"scene":场景名,"desc":一句话方案描述（含花材与支数，如「玫瑰×10 配满天星×3」）,"effect_prompt":"生图 prompt（描述花材/色彩/形态/包装，与方案一致）","design":{"main_flowers":[{"name":花名,"role":"主花","flower_language":[花语],"qty":支数}],"fillers":[{"name":花名,"role":"填充","qty":支数}],"foliage":[{"name":叶材名,"role":"叶材","qty":支数}],"color_scheme":[颜色],"packaging":包装名,"meaning":寓意文案,"diy_steps":DIY 步骤(数组，需具体到每种花材的修剪方式与数量，如「玫瑰×10 斜剪45°去刺去叶」),"care_tips":养护贴士,"card_message":贺卡文案,"difficulty":制作难度(仅限 入门/进阶/高手),"est_time":预计耗时分钟数(整数),"shelf_life":保鲜期(收到后可养几天,如"约 5-7 天"),"suitable_for":[适宜人群标签],"caution":禁忌或提醒(如花粉过敏慎选),"mood_tags":[情绪标签(如 治愈/热烈/宁静)]}}。要求：花材必须从【候选花材】中选取真实名称；配色与风格须与知识库一致；每种花材务必给出具体支数 qty（按预算合理分配，主花 6-16 支、配材/叶材 1-4 支）；diy_steps 要具体到每种花材怎么修剪（斜剪/去刺/去叶/摘雄蕊）、怎么装饰；若用户未指定某维度，按花语与场景合理默认，不要留空。'
        user = f'用户需求：{requirements}\n\n{knowledge}'
        resp = call_llm([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], response_format={'type': 'json_object'})
        content = resp.choices[0].message.content
        llm_plan = json.loads(content)
        plan = _merge_plan(baseline, llm_plan)
        plan['plan_id'] = baseline['plan_id']
        plan['version'] = baseline.get('version', 1)
        plan['parent_id'] = baseline.get('parent_id')
        plan['diy'] = True
        return plan
    except Exception:
        logger.exception('[design] LLM 语义生成失败，回退规则引擎')
        return baseline

def design_diy_plan(requirements: str) -> dict:
    """设计一份结构化 DIY 花艺方案（RAG + LLM 语义生成，规则引擎兜底）。

    链路：RAG 检索知识库 → DeepSeek 生成语义化方案 → 规则引擎 _build_plan 补全结构/兜底。
    返回可供 UI 渲染、生图与下单承接的结构化 dict。
    """
    return design_with_llm(requirements)

def revise_with_llm(plan: str, feedback: str) -> dict:
    """语义化改版：RAG 检索 + DeepSeek 基于已有方案与反馈调整，规则引擎兜底。

    反馈里明确要改的（预算/风格/色系/移除花材）必须落实；未提及维度保持原方案。
    """
    original = _parse_plan(plan)
    dims = _dims_from_plan(original)
    fb = _extract_feedback(feedback)
    dims.update(fb['dims'])
    baseline = _build_plan(dims, version=original.get('version', 1) + 1, parent_id=original.get('plan_id'), exclude_flowers=fb['exclude'])
    try:
        knowledge = _retrieve_for_design(f"{original.get('desc', '')} {feedback}")
        system = '你是资深花艺设计师。基于【已有方案】与【用户反馈】调整出一版新方案，只输出 JSON、不要额外解释。字段须严格同设计：{"name":方案名,"style":风格标签,"recipient":收礼人,"occasion":场景或节日,"scene":场景名,"desc":一句话方案描述,"effect_prompt":"生图 prompt（与方案一致）","design":{"main_flowers":[{"name":花名,"role":"主花","flower_language":[花语]}],"fillers":[{"name":花名,"role":"填充"}],"foliage":[{"name":叶材名,"role":"叶材"}],"color_scheme":[颜色],"packaging":包装名,"meaning":寓意文案,"diy_steps":DIY 步骤,"care_tips":养护贴士,"card_message":贺卡文案,"difficulty":制作难度(仅限 入门/进阶/高手),"est_time":预计耗时分钟数(整数),"shelf_life":保鲜期(收到后可养几天,如"约 5-7 天"),"suitable_for":[适宜人群标签],"caution":禁忌或提醒(如花粉过敏慎选),"mood_tags":[情绪标签(如 治愈/热烈/宁静)]}}。要求：反馈明确要改的维度必须落实；花材从知识库真实名称选；未提及的维度保持原方案，不要随意改动。'
        user = f'已有方案：{json.dumps(original, ensure_ascii=False)}\n用户反馈：{feedback}\n\n{knowledge}'
        resp = call_llm([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], response_format={'type': 'json_object'})
        llm_plan = json.loads(resp.choices[0].message.content)
        new_plan = _merge_plan(baseline, llm_plan)
        new_plan['plan_id'] = baseline['plan_id']
        new_plan['version'] = original.get('version', 1) + 1
        new_plan['parent_id'] = original.get('plan_id')
        new_plan['diy'] = True
        return json.dumps(new_plan, ensure_ascii=False)
    except Exception:
        logger.exception('[revise] LLM 语义改版失败，回退规则引擎')
        return json.dumps(baseline, ensure_ascii=False)

@register_tool(name='respond_to_user', description='当你准备好向用户输出本轮最终回复时，必须调用该工具结束本轮对话。携带：reply（自然语言回复）、ui（UI 动作类型）、data（按 ui 类型填充）、stage（协商后的下一业务阶段）、intent（你判断的用户本轮真实意图）。', parameters={'type': 'object', 'properties': {'reply': {'type': 'string', 'description': '给用户的自然语言回复'}, 'ui': {'type': 'string', 'enum': [e.value for e in UIType], 'description': '小程序渲染的 UI 动作类型'}, 'data': {'type': 'object', 'description': '按 ui 类型约定的结构化数据'}, 'stage': {'type': 'string', 'description': '下一业务阶段，如 analyze/select_mode/view_plan/diy_design/image_gen/shop_recommend/done'}, 'intent': {'type': 'string', 'enum': ['buying', 'qa', 'chitchat', 'design', 'other'], 'description': '用户本轮真实意图：buying=有购买/挑选花束的明确意图；qa=问花卉/花艺知识或咨询（花期/养护/寓意/送什么花好）；chitchat=纯闲聊寒暄；design=要 DIY 定制专属花束；other=其他。判定依据是用户『想干什么』，不是本轮是否调了工具。'}}, 'required': ['reply', 'ui', 'data', 'stage']}, tags=['meta'])
def respond_to_user(reply: str='', ui: str='text', data: dict | None=None, stage: str='analyze', intent: str='other') -> str:
    """终结工具：模型以此结束本轮，参数由 agent 提取并校验后返回前端。"""
    if intent not in ('buying', 'qa', 'chitchat', 'design', 'other'):
        intent = 'other'
    return {'reply': reply, 'ui': ui, 'data': data or {}, 'stage': stage, 'intent': intent}

