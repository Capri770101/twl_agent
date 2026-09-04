"""agent 包。

导入本包会顺带导入全部内建工具模块，使其 `@register_tool` 装饰器生效、
填满 `agent.toolkit.TOOL_REGISTRY`——agent 的 function-calling 工具清单和
system prompt 里的工具说明书都依赖这张注册表。

历史 bug：这些模块此前没有任何地方 import，导致 TOOL_REGISTRY 恒为空，
LLM 收不到任何工具定义（只能输出模仿提示词的 JSON 文本）。勿删除下面的导入。
"""
from __future__ import annotations

from agent import data_tools as _data_tools  # noqa: F401
from agent import diy_tools as _diy_tools  # noqa: F401
from agent import memory_tools as _memory_tools  # noqa: F401
from agent import tools as _tools  # noqa: F401
from agent.skills import skill_order as _skill_order  # noqa: F401
from agent.skills import skill_greeting as _skill_greeting  # noqa: F401

__all__ = ['tools', 'toolkit']
