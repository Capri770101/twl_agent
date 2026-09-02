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
MCP_SAFE_TOOL_NAMES = frozenset({
    'retrieve_knowledge', 'search_plans', 'get_plan_detail', 'search_shops',
    'match_shop_items', 'generate_diy_plan', 'revise_diy_plan',
})


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
    """生成 OpenAI function-calling 的 tools 定义。"""
    return [{'type': 'function', 'function': {'name': s.name, 'description': s.description, 'parameters': s.parameters}} for s in TOOL_REGISTRY.values()]


def generate_tool_manual() -> str:
    """生成中文工具说明书，注入 system prompt。"""
    lines = ['你当前可以使用的工具（需要时以 JSON 或 function call 形式调用）：']
    for s in TOOL_REGISTRY.values():
        params = ', '.join((f"{k}: {v.get('type', 'any')}" for k, v in s.parameters.get('properties', {}).items()))
        lines.append(f'- {s.name}({params})：{s.description}')
    return '\n'.join(lines)


async def execute_tool(name: str, arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> tuple[str, str]:
    """执行工具，返回 (结果字符串, 状态 ok|error)。"""
    spec = TOOL_REGISTRY.get(name)
    if not spec:
        return (f'未知工具: {name}', 'error')
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
