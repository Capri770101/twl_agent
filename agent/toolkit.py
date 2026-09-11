"""工具基础设施层：注册表与执行入口。"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
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

    Returns:
        可见的 :class:`ToolSpec` 列表（顺序与注册顺序一致）。
    """
    specs = get_tool_specs()
    if ops_tools_enabled():
        return specs
    return [s for s in specs if OPS_TOOL_TAG not in s.tags]


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


def to_openai_tools() -> list[dict[str, Any]]:
    """生成 OpenAI function-calling 的 tools 定义（仅当前会话可见的工具）。"""
    return [{'type': 'function', 'function': {'name': s.name, 'description': s.description, 'parameters': s.parameters}} for s in visible_tool_specs()]


def generate_tool_manual() -> str:
    """生成中文工具说明书，注入 system prompt（仅当前会话可见的工具）。"""
    lines = ['你当前可以使用的工具（需要时以 JSON 或 function call 形式调用）：']
    for s in visible_tool_specs():
        params = ', '.join((f"{k}: {v.get('type', 'any')}" for k, v in s.parameters.get('properties', {}).items()))
        lines.append(f'- {s.name}({params})：{s.description}')
    return '\n'.join(lines)


async def execute_tool(name: str, arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> tuple[str, str]:
    """执行工具，返回 (结果字符串, 状态 ok|error)。"""
    spec = TOOL_REGISTRY.get(name)
    if not spec:
        return (f'未知工具: {name}', 'error')
    # 运维工具在未开放时拒绝执行：即使模型幻觉调用（工具定义已不暴露），也挡在入口。
    if OPS_TOOL_TAG in spec.tags and not ops_tools_enabled():
        return ('该工具当前不可用', 'error')
    try:
        kwargs = dict(arguments or {})
        if spec.inject_context:
            kwargs['_context'] = context
        if inspect.iscoroutinefunction(spec.func):
            result = await spec.func(**kwargs)
        else:
            result = spec.func(**kwargs)
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
