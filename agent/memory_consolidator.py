"""会话记忆自动固化（L2 自我进化路径）。

问题：长期记忆此前只能靠 LLM 主动调 save_memory / save_user_profile，
实测下来它经常不调——用户说过一次「送妈妈、预算 300」，下次就忘了。

方案：每轮对话结束后，按需异步跑一次轻量提炼，把用户**明确表达**的偏好
写进 user_preferences；下次对话由 _build_system 的「用户偏好记忆」段直接读到。

约束（缺一不可）：
- **不阻塞主响应**：由路由以后台任务触发，主流程不等它；
- **失败静默**：只记日志，绝不抛给调用方，不影响对话本身；
- **宁缺毋滥**：只提取明确表达的信息，没有就一条都不写，绝不推测补全；
- **有节流**：累积若干条新消息才跑一次，避免每轮都多一次 LLM 调用；
- **白名单**：只接受约定的 key 并限制值长度，防止脏数据进长期记忆。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from agent.engine.llm import call_llm
from backend.config import settings
from backend.storage import memory as mem_store

logger = logging.getLogger('memory_consolidator')

# 允许的长期偏好键（与 save_user_profile_from_requirement 一致，另加忌讳项）
_ALLOWED_KEYS = frozenset({
    'preferred_recipient', 'preferred_occasion', 'preferred_style',
    'preferred_mood', 'preferred_colors', 'budget_level', 'avoided_flowers',
})
_MAX_VALUE_LEN = 60
_MAX_INPUT_CHARS = 1500
_FLAG_KEY = 'consolidated_count'


def _threshold() -> int:
    """累积多少条新消息提炼一次（来自配置，异常时退回默认 4）。"""
    try:
        return max(1, int(getattr(settings, 'MEMORY_CONSOLIDATE_EVERY', 4) or 4))
    except (TypeError, ValueError):
        return 4


def _enabled() -> bool:
    return bool(getattr(settings, 'MEMORY_CONSOLIDATE_ENABLED', True))

_SYSTEM_PROMPT = (
    '你是「记忆提炼器」。下面是一段花艺选购对话。\n'
    '只提取**用户明确说过**的长期偏好，输出 JSON，不要任何解释。\n'
    '\n'
    '可选字段（只输出有把握的，没把握就省略该字段）：\n'
    '- preferred_recipient 常送的人（妈妈、女朋友、老师…）\n'
    '- preferred_occasion 常见场合（生日、探病、开业…）\n'
    '- preferred_style 偏好风格（韩式、复古、简约…）\n'
    '- preferred_mood 想要的感觉（温柔、热烈、清新…）\n'
    '- preferred_colors 偏好色系（粉色、白绿、香槟…）\n'
    '- budget_level 预算水平（只写数字，如 300）\n'
    '- avoided_flowers 明确表示不喜欢或不要的花材（百合、康乃馨…）\n'
    '\n'
    '规则：\n'
    '- 用户只是随口一问、没有表达长期偏好时，返回 {}\n'
    '- 不要推测、不要补全、不要写你觉得合理但用户没说的内容\n'
    '- 只输出 JSON，例如 {"preferred_recipient":"妈妈","budget_level":"300"}'
)

_JSON_BLOCK = re.compile(r'\{[^{}]*\}', re.S)


def _extract_json_object(text: str) -> dict[str, Any]:
    """从 LLM 输出里抠出 JSON 对象；解析不出就返回空字典（宁缺毋滥）。"""
    raw = (text or '').strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        pass
    # 模型可能夹带说明文字，退一步取第一个 {...} 块
    m = _JSON_BLOCK.search(raw)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _sanitize(raw: dict[str, Any]) -> dict[str, str]:
    """白名单过滤 + 长度限制，避免脏数据或超长文本写进长期记忆。"""
    clean: dict[str, str] = {}
    for key, value in (raw or {}).items():
        if key not in _ALLOWED_KEYS or value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if len(text) > _MAX_VALUE_LEN:
            text = text[:_MAX_VALUE_LEN]
        clean[key] = text
    return clean


async def _load_recent_dialog(user_id: str, session_id: str, limit: int = 12) -> str:
    """取最近若干条对话文本，拼成给提炼器看的输入。"""
    history = await mem_store.load_history(session_id, limit)
    lines: list[str] = []
    for msg in history:
        role = msg.get('role')
        content = (msg.get('content') or '').strip()
        if role not in ('user', 'assistant') or not content:
            continue
        # 单条截断：长卡片文案对提炼没有意义，只会撑大输入
        if len(content) > 200:
            content = content[:200] + '…'
        lines.append('{}：{}'.format('用户' if role == 'user' else '助手', content))
    text = '\n'.join(lines)
    return text[-_MAX_INPUT_CHARS:]


async def consolidate_now(user_id: str, session_id: str) -> dict[str, str]:
    """立即跑一次提炼并写入长期记忆。返回本次写入的键值对（可能为空）。"""
    if not user_id or not session_id:
        return {}
    dialog = await _load_recent_dialog(user_id, session_id)
    if not dialog:
        return {}

    def _call() -> str:
        resp = call_llm(
            [
                {'role': 'system', 'content': _SYSTEM_PROMPT},
                {'role': 'user', 'content': dialog},
            ]
        )
        return (resp.choices[0].message.content or '').strip()

    # call_llm 是同步阻塞调用，放到线程里避免卡住事件循环
    raw_text = await asyncio.to_thread(_call)
    clean = _sanitize(_extract_json_object(raw_text))
    if not clean:
        logger.info('[consolidate] user=%s 未提炼出明确偏好，跳过写入', user_id)
        return {}
    for key, value in clean.items():
        await mem_store.set_long_term(user_id, key, value)
    logger.info('[consolidate] user=%s 写入长期偏好 %s', user_id, list(clean))
    return clean


async def maybe_consolidate(user_id: str, session_id: str) -> None:
    """按节流条件决定是否提炼；供路由以后台任务调用，**绝不抛异常**。

    节流：每累积 _NEW_MESSAGES_PER_ROUND 条新消息跑一次（按会话计数），
    避免每轮对话都多一次 LLM 调用。
    """
    try:
        if not _enabled():
            return
        if not user_id or not session_id:
            return
        with mem_store.transaction() as conn:
            row = conn.execute(
                'SELECT count(*) AS n FROM messages WHERE session_id = ?', (session_id,)
            ).fetchone()
        total = int((row or {}).get('n') or 0)
        last_raw = await mem_store.get_session_json(user_id, f'flag:{session_id}', _FLAG_KEY)
        try:
            last = int(last_raw or 0)
        except (TypeError, ValueError):
            last = 0
        if total - last < _threshold():
            return
        await consolidate_now(user_id, session_id)
        await mem_store.set_session_flag(user_id, session_id, _FLAG_KEY, str(total))
    except Exception:
        # 记忆提炼失败绝不能影响对话：只落日志
        logger.exception('[consolidate] 提炼失败（已忽略）user=%s', user_id)
