"""工具基础设施层：注册表与执行入口。"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger('tools')


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    inject_context: bool = False
    tags: set[str] = field(default_factory=set)


TOOL_REGISTRY: dict[str, ToolSpec] = {}

# MCP/外部调用只能显式选择安全工具；不要把 TOOL_REGISTRY 全量导出。
# 2026-09 重构：本地商品镜像查询（search_plans/get_plan_detail/search_shops/
# match_shop_items）已删除，由只读 platform_db_query_entity 取代。
MCP_SAFE_TOOL_NAMES = frozenset({
    'retrieve_knowledge', 'generate_diy_plan', 'revise_diy_plan',
    'platform_db_query_entity', 'platform_mapping_get_active',
    'suggest_greetings', 'render_greeting_card',
})

# 运维 / 接入期工具标记。
#
# 这类工具面向「平台接入配置」（结构发现、映射草案与审核状态变更），终端用户对话用不到，
# 且其中 platform_mapping_set_status 能改映射审核状态——属会话不该触达的权限面。
# 默认**不出现在 C 端会话的工具列表**中；仅在部署接入期用 ENABLE_OPS_TOOLS=true 临时开放。
# 收益：① 收窄权限面；② 减少工具选择空间（21 → 13），提升选工具准确率；③ 减小 tools schema 体积。
OPS_TOOL_TAG = 'ops'


def ops_tools_enabled() -> bool:
    """运维工具是否对当前会话开放（由 ENABLE_OPS_TOOLS 控制，默认关闭）。"""
    try:
        from backend.config import settings
        return bool(settings.ENABLE_OPS_TOOLS)
    except Exception:
        return False


def visible_tool_specs() -> list[ToolSpec]:
    """返回当前会话可见的工具列表。

    默认排除带 ``ops`` 标签的运维/接入工具；``ENABLE_OPS_TOOLS=true`` 时全量返回。
    若配置了 ``PLATFORM_ALLOWED_ENTITIES``，再把 ``platform_db_query_entity`` 的
    ``entity`` 选项收窄到白名单内的实体（体验/演示实例用它关掉店铺查询）。

    Returns:
        可见的 :class:`ToolSpec` 列表（顺序与注册顺序一致）。
    """
    specs = get_tool_specs()
    from agent.engine.tool_scope import active_tools
    scope = active_tools.get()
    if scope is not None:
        specs = [spec for spec in specs if spec.name in scope]
    from backend.data_gateway.access import cache_scope
    if not ops_tools_enabled() or cache_scope() is not None:
        specs = [s for s in specs if OPS_TOOL_TAG not in s.tags]
    allowed = allowed_entities() or {'plan', 'shop'}
    if allowed:
        kept = [e for e in _PLATFORM_ENTITIES if e in allowed]
        if not kept:
            # 白名单里没有任何合法实体（配置写错，如 PLATFORM_ALLOWED_ENTITIES=none）
            # → **fail-closed**：直接把查询工具下架。绝不能回退成「不限制」——
            # 那会让体验实例的收窄静默失效，等于白配。
            logger.warning('[toolkit] PLATFORM_ALLOWED_ENTITIES=%s 无合法实体，已下架平台查询工具', sorted(allowed))
            specs = [s for s in specs if s.name != 'platform_db_query_entity']
        else:
            specs = [
                _narrow_entity_schema(s, allowed) if s.name == 'platform_db_query_entity' else s
                for s in specs
            ]
    return specs


# 平台数据支持的实体（与 data_tools / http_source 对齐）。
_PLATFORM_ENTITIES: tuple[str, ...] = ('plan', 'shop')


def allowed_entities() -> set[str]:
    """平台数据「可查实体」白名单；返回空集合表示**不限制**（生产默认）。

    用途：体验/演示实例只需要「生成方案 + 给建议」这条主线，把店铺查询关掉，
    避免向体验客户暴露店铺与成交链路。配置见 ``PLATFORM_ALLOWED_ENTITIES``。

    Returns:
        允许多查询的实体名集合（小写）；未配置时为空集合。
    """
    try:
        from backend.config import settings
        raw = str(getattr(settings, 'PLATFORM_ALLOWED_ENTITIES', '') or '').strip()
    except Exception:
        return set()
    if not raw:
        return set()
    items = raw.replace('，', ',').split(',')
    return {x.strip().lower() for x in items if x.strip()}


def _narrow_entity_schema(spec: ToolSpec, allowed: set[str]) -> ToolSpec:
    """把 ``platform_db_query_entity`` 的 entity 选项收窄到白名单内的实体。

    为什么改 schema 而不是只做执行期拦截：模型**看不到** ``shop`` 这个取值，就不会
    去查店铺，也就不会产出店铺卡或「去哪家店买」的话术；执行期拦截只作兜底。
    描述里的「entity 取值：…」枚举串一并收窄，避免 schema 与说明自相矛盾。

    Args:
        spec: 原工具定义。
        allowed: 允许的实体集合。

    Returns:
        收窄后的工具定义；``allowed`` 与支持实体无交集时原样返回。
    """
    kept = [e for e in _PLATFORM_ENTITIES if e in allowed]
    if not kept:
        return spec
    params = copy.deepcopy(spec.parameters)
    props = params.get('properties') or {}
    if isinstance(props.get('entity'), dict):
        props['entity'] = {**props['entity'], 'description': '/'.join(kept), 'enum': kept}
    description = re.sub(
        r'entity 取值：[^；]*；',
        f'entity 取值：{"、".join(kept)}；',
        spec.description,
    )
    return replace(spec, description=description, parameters=params)


def register_tool(name: str, description: str, parameters: dict[str, Any], inject_context: bool = False, tags: list[str] | None = None) -> Callable[[Callable], Callable]:
    """装饰器：把函数登记进 TOOL_REGISTRY。"""

    def deco(func: Callable) -> Callable:
        TOOL_REGISTRY[name] = ToolSpec(name=name, description=description, parameters=parameters, func=func, inject_context=inject_context, tags=set(tags or []))
        return func

    return deco


def get_tool_specs() -> list[ToolSpec]:
    return list(TOOL_REGISTRY.values())


def get_mcp_tool_specs(allowed: set[str] | None = None) -> list[ToolSpec]:
    """返回显式白名单工具，供未来 MCP bridge 使用。"""
    names = MCP_SAFE_TOOL_NAMES if allowed is None else MCP_SAFE_TOOL_NAMES.intersection(allowed)
    return [spec for spec in get_tool_specs() if spec.name in names]


def to_openai_tools(*, compact: bool = False) -> list[dict[str, Any]]:
    """生成当前会话工具定义。

    ``compact`` 只压缩冗长的说明文字，保留完整函数名和 JSON Schema。
    详细规则已经在 system prompt 中，避免每个 ReAct 轮次重复携带同一大段说明。
    """
    tools = []
    for spec in visible_tool_specs():
        description = spec.description
        if compact:
            description = {
                'respond_to_user': '结束本轮并返回纯文字回复。填写真实的意图、确认、生图、换批及缺失需求信号；不确定用 none/false/[]，不编造卡片数据。参数含义见 schema。',
                'show_plan_card': '展示工具已产出的商品或 DIY 方案卡并结束本轮；plans 使用真实工具结果。reply 放在参数第一位，供即时流式展示。填写真实理解信号。',
                'show_options': '给用户 2-4 个可点击选项并结束本轮，每项建议不超过12字。reply 先说明选择目的，再给 options；填写真实理解信号。',
            }.get(spec.name, description)
        tools.append({'type': 'function', 'function': {
            'name': spec.name, 'description': description, 'parameters': spec.parameters,
        }})
    return tools


def generate_tool_manual() -> str:
    """生成中文工具说明书（**当前不再注入 system prompt**）。

    工具定义已由 function-calling 的 ``tools`` 参数完整提供（含每个参数的 JSON Schema），
    本函数输出与其重复、且信息更少。经 A/B 实测（2026-09-11）：移除注入后工具选择无退化、
    输入字符 -29.6%。保留本函数作为**文本兜底**——当某 provider 不支持 function calling 时，
    可用它把「有哪些工具」以文字形式告知模型。
    """
    lines = ['你当前可以使用的工具（需要时以 JSON 或 function call 形式调用）：']
    for s in visible_tool_specs():
        params = ', '.join((f"{k}: {v.get('type', 'any')}" for k, v in s.parameters.get('properties', {}).items()))
        lines.append(f'- {s.name}({params})：{s.description}')
    return '\n'.join(lines)


async def execute_tool(name: str, arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> tuple[str, str]:
    """执行工具，返回 (结果字符串, 状态 ok|error)。"""
    from backend.execution import checkpoint
    checkpoint()
    spec = TOOL_REGISTRY.get(name)
    from agent.engine.tool_scope import active_tools
    scope = active_tools.get()
    if scope is not None and name not in scope:
        return ('本轮为养护问答，请使用知识检索并直接回答', 'error')
    if not spec:
        return (f'未知工具: {name}', 'error')
    # 运维工具在未开放时拒绝执行：即使模型幻觉调用（工具定义已不暴露），也挡在入口。
    from backend.data_gateway.access import cache_scope
    if OPS_TOOL_TAG in spec.tags and (not ops_tools_enabled() or cache_scope() is not None):
        return ('该工具当前不可用', 'error')
    try:
        kwargs = dict(arguments or {})
        if spec.inject_context:
            kwargs['_context'] = context
        if inspect.iscoroutinefunction(spec.func):
            result = await spec.func(**kwargs)
        else:
            # 同步工具丢线程池执行。为什么必须这样：同步 I/O（如平台 REST 查询走
            # 同步 httpx.get）会**阻塞整个事件循环** —— 一个用户的工具执行期间，
            # 所有并发请求（含其它用户的对话）都被卡住。to_thread 让事件循环继续服务。
            # 线程安全前提（已核对）：http_source._CACHE / shop_materials._LOCK 均带锁，
            # retrieve_knowledge 只读本地知识库。
            result = await asyncio.to_thread(spec.func, **kwargs)
        checkpoint()
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                if 'ok' in parsed and parsed.get('ok') is False:
                    return (result, 'error')
                if 'ok' not in parsed and 'error' in parsed:
                    return (result, 'error')
        except (json.JSONDecodeError, TypeError):
            pass
        return (result, 'ok')
    except Exception as exc:
        logger.exception('[tools] 执行 %s 失败', name)
        return (f'工具执行失败: {exc}', 'error')
