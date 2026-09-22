"""保守识别独立养护问题；混合需求保留完整工具集。"""
from contextvars import ContextVar
from functools import wraps
import re

CARE_TOOLS = frozenset({'retrieve_knowledge', 'respond_to_user', 'search_history', 'get_user_profile'})
active_tools = ContextVar('active_agent_tools', default=None)
active_entities = ContextVar('active_platform_entities', default=None)


def is_standalone_care(message: str) -> bool:
    text = str(message or '').strip()
    # 仅移除明确的拒绝附加业务语句，其余购买/改版/图片表达均保守回退。
    text = re.sub(r'(?:不要|不用|无需)(?:方案和图片|方案和效果图|方案|图片|效果图)', '', text)
    if any(w in text for w in ('推荐', '买', '送', '预算', '方案', '配色', '定制', '订单', '店', '价格', '图', '换成', '改成', '贺卡', '记住')):
        return False
    return len(text) <= 120 and any(w in text for w in ('怎么养', '如何养', '养护', '换水', '剪根', '保鲜', '醒花'))


def scoped_tools(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        from backend.config import settings
        message = kwargs.get('message', args[2] if len(args) > 2 else '')
        from agent.engine.intent import classify
        route = classify(message) if settings.AGENT_INTENT_ROUTING_ENABLED else None
        scope = route.tools if route and route.tools is not None else (CARE_TOOLS if settings.CARE_TOOL_SCOPE_ENABLED and is_standalone_care(message) else None)
        entity_scope = frozenset({'plan'}) if route and route.name == 'buying' else None
        token = active_tools.set(scope)
        entity_token = active_entities.set(entity_scope)
        try:
            return await func(*args, **kwargs)
        finally:
            active_entities.reset(entity_token)
            active_tools.reset(token)
    return wrapped
