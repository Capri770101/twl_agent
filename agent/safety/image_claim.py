"""生图声明护栏：没有可信 task_id 时不得声称图片已生成。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger('agent')

# 完成态必须与图词相邻；宽松共现会误伤「我已经生成了方案」一类回复。
_IMAGE_CLAIM_STRONG = (
    r'已(?:经)?(?:提交|生成|出图|就绪|完成|安排|发起|排上|排队|开始|出)'
    r'|正在(?:生成|出图|处理|渲染|跑|做)'
    r'|生成中|生成好了|出图中|制作中|处理中|渲染中'
    r'|马上(?:就好|出图|生成好|出来)'
)
_IMAGE_CLAIM_INPROGRESS = r'在跑|跑起来|跑上了|跑着呢'
_IMAGE_CLAIM_WORDS = _IMAGE_CLAIM_STRONG + r'|' + _IMAGE_CLAIM_INPROGRESS

_IMAGE_CLAIM_PATTERNS = (
    re.compile(r'(?:效果图|生图|图片|参考图|出图)[^。！？\n]{0,10}(?:' + _IMAGE_CLAIM_WORDS + r')'),
    re.compile(r'(?:已提交|已生成|生成中|正在生成|已出图)[^。！？\n]{0,10}'
               r'(?:效果图|生图|图片|参考图|出图)'),
    re.compile(r'(?:这张|这张图|上图|下图|配图)[^。！？\n]{0,20}(?:生成|参考图|效果图)'),
)
_IMAGE_FAIL_WORDS = ('没有生成', '未生成', '无法生成', '生成失败', '没能生成', '还未生成', '尚未生成')

_IMAGE_UNVERIFIED_REPLY = (
    '抱歉，这条效果图**并没有真的生成成功**——我这边没有拿到生图任务，刚才的说法不准确。\n'
    '要我再试一次吗？直接说「用这个方案出一张效果图」就行。'
)


def _image_task_created(tool_log: list[Any]) -> bool:
    """成功调用且返回 task_id 才算真正提交了生图任务。"""
    return bool(_extract_image_task(tool_log).get('task_id'))


def _extract_image_task(tool_log: list[Any]) -> dict[str, Any]:
    """提取最近一次成功生图任务的 task_id / poll / result_url。"""
    for tc in reversed(tool_log or []):
        if getattr(tc, 'status', '') != 'ok' or getattr(tc, 'name', '') != 'generate_effect_image':
            continue
        try:
            result = json.loads(tc.result) if isinstance(tc.result, str) else tc.result or {}
        except (json.JSONDecodeError, TypeError):
            result = {}
        if isinstance(result, dict) and result.get('task_id'):
            out: dict[str, Any] = {'task_id': result['task_id']}
            if result.get('poll'):
                out['poll'] = result['poll']
            if result.get('result_url'):
                out['result_url'] = result['result_url']
            return out
    return {}


def _claims_image_done(reply: str) -> bool:
    """回复是否声称已出图、且并非在如实说明失败。"""
    text = reply or ''
    if not text:
        return False
    for clause in re.split(r'[。！？!?；;，,\n]+', text):
        if any(p.search(clause) for p in _IMAGE_CLAIM_PATTERNS):
            if not any(w in clause for w in _IMAGE_FAIL_WORDS):
                return True
    return False


def _needs_image_nudge(tool_log: list[Any], reply: str) -> bool:
    """未提交真实任务却声称已出图时，要求模型重答。"""
    return not _image_task_created(tool_log) and _claims_image_done(reply)


def _image_nudge_text() -> str:
    """本轮纠正指令（不落库）。"""
    return (
        '[系统校验未通过] 你刚才声称效果图「已提交 / 已生成 / 生成中」，但**本轮并没有任何真实的'
        '生图任务**——用户会去找一张根本不存在的图，这是最严重的错误。\n'
        '现在请二选一：\n'
        '· 用户确实想要效果图 → 调用 generate_effect_image(plan="latest_diy")，'
        '拿到返回的 task_id 之后再如实告知「已提交，生成中」；\n'
        '· 生图不可用（如未找到可生图的方案）→ **如实说明**原因，并给出替代建议。\n'
        '⚠️ 没有 task_id 就绝不能说任务或图的存在，也不要描述一张「已生成的图」长什么样。'
    )


def _finalize_image_claim(reply: str, tool_log: list[Any]) -> str:
    """模型重答后仍谎称已出图，使用确定性文案如实告知。"""
    if _image_task_created(tool_log) or not _claims_image_done(reply):
        return reply
    logger.warning('[agent] 回复谎称已生成效果图但本轮无生图任务，已替换为如实说明')
    return _IMAGE_UNVERIFIED_REPLY
