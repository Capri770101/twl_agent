"""工具作用域的会话级上下文变量。

v1 退役（2026-10-14）：关键词层（classify / scoped_tools / is_standalone_care）已删除，
工具圈由 v2 脊柱按理解层声明的意图查 routing.json 策略表后直接 ``active_tools.set``。
本模块只保留两个 ContextVar——toolkit（可见性过滤 + 执行拦截）与 spine（设圈）共用。
"""
from contextvars import ContextVar

#: 本轮允许模型使用的工具名集合；None = 不缩圈（全量可见工具）。
active_tools = ContextVar('active_agent_tools', default=None)

#: 本轮允许查询的平台实体集合（plan/shop）；None = 不限制。
active_entities = ContextVar('active_platform_entities', default=None)
