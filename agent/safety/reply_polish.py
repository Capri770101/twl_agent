"""回复发布前的确定性清洗：伪造电话、markdown 噪声、工具报错脱敏。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger('agent')

_PHONE_IN_TEXT_RE = re.compile(r'(?:1[3-9]\d{9}|0\d{2,3}-\d{7,8}|\d{3,4}-\d{7,8})')

# 上游 LLM 内容审核的英文拒答串（概率性误伤正常业务消息，如「我感冒了吃什么药」
# 返回 "[Nudity Detected] I cannot answer this question."）。特征：方括号英文标签
# + cannot/won't answer 的固定句式，且整段以它开头。命中即替换为中文角色内话术。
_UPSTREAM_MODERATION_BLOCK_RE = re.compile(
    r'^\[?[A-Za-z ]{3,40}(?:Detected|Detected\]|Blocked|Flagged|Filter)'
    r'.{0,40}(?:cannot|can\'t|unable to|won\'t)\s+(?:answer|respond|assist|comply)',
    re.IGNORECASE,
)


def _guard_merchant_phone(tool_log, reply: str) -> str:
    """contact_merchant 成功后，回复里只允许出现真实的 service_phone。

    模型会在文案里编造「0571-88886666」这类假号码（实测 3 次中 2 次），
    提示词约束挡不住——这里是确定性护栏：
    - 提取本轮 contact_merchant 成功结果里的 service_phone；
    - 回复中出现的其他号码一律替换为真实号码（无从提取时删号并提示小程序查看）。
    """
    if not reply or not tool_log:
        return reply
    real_phone = None
    for record in tool_log:
        if record.name != 'contact_merchant' or record.status != 'ok':
            continue
        try:
            payload = json.loads(record.result) if isinstance(record.result, str) else record.result
            if isinstance(payload, dict) and payload.get('ok') is True:
                real_phone = (payload.get('data') or {}).get('service_phone')
        except (ValueError, TypeError, AttributeError):
            continue
    if not real_phone:
        return reply
    real_phone = str(real_phone)
    found = _PHONE_IN_TEXT_RE.findall(reply)
    fabricated = [p for p in found if p != real_phone and p.replace('-', '') != real_phone]
    if not fabricated:
        return reply
    logger.warning('[agent] contact_merchant 回复含编造电话 %s（真实=%s），已纠正', fabricated, real_phone)
    for fake in set(fabricated):
        reply = reply.replace(fake, real_phone)
    return reply


def _clean_reply(text: str) -> str:
    """清理智能体回复里的 markdown 噪声，让前端纯文本渲染更整洁。

    前端不渲染 markdown，因此 ``**加粗**`` 会原样显示成 ``**``；这里统一去除
    ``**`` 与行首 ``#`` 标题符，并把连续空行折叠为单空行，保留有序列表等可读结构。
    """
    if not text:
        return text
    # 上游 LLM 内容审核偶发误伤（本地实测 2026-10-09：问「我感冒了吃什么药」
    # 概率性返回英文审核拒答文案）。这类英文拒答串对中文用户是彻底的答非所问，
    # 也暴露内部机制；替换为角色内中文话术，不改变其余回复内容。
    t = text.strip()
    if _UPSTREAM_MODERATION_BLOCK_RE.search(t):
        logger.warning('[agent] 命中上游审核误伤文案，已替换为中文兜底（原文前60字符=%r）', t[:60])
        return ('这个问题我先不展开哈～我是花艺小助手，医药、法律这类专业问题'
                '建议找对应的专业人士；选花、搭配、养护的事儿尽管找我 🌸')
    text = text.replace('**', '')
    text = re.sub('(?m)^#{1,6}\\s*', '', text)
    text = re.sub('\\n{3,}', '\n\n', text)
    return text.strip()


def _tool_error_result(tool_name: str, exc: BaseException | None = None) -> str:
    """把工具失败转成**脱敏后**的 tool 消息（2026-09-20，review 第 6 条）。

    原先回填给模型的是 ``f'工具执行失败: {item}'`` —— 直接把异常 ``repr`` 塞进上下文，
    可能带出内部路径 / 参数 / 堆栈片段；模型一旦复述，这些就进了用户可见的 `reply`。
    现在只给「哪类失败 + 该怎么办」，**原始异常只落服务端日志**（调用处已有
    ``logger.exception``，保留完整 traceback 便于排查）。

    Args:
        tool_name: 失败的工具名。
        exc: 原始异常（可为 None，表示非异常原因导致的失败）。

    Returns:
        JSON 字符串，形如 ``{"error": "查询超时", "tool": "platform_db_query_entity"}``。
    """
    hint = '该工具暂时不可用，可换一种方式或改用其它工具'
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        hint = '查询超时，可缩小范围后重试'
    elif isinstance(exc, (KeyError, ValueError, TypeError)):
        hint = '参数不被接受，请调整参数后重试（或改用其它工具）'
    elif isinstance(exc, (ConnectionError, OSError)):
        hint = '外部数据源暂时连不上，可稍后重试或如实告知用户查不到'
    payload: dict[str, Any] = {'error': hint, 'tool': tool_name, 'failed': True}
    return json.dumps(payload, ensure_ascii=False)
