"""QA 空壳回复兜底回归（2026-10-08，源自 2026-09-22 真实模型评测 qa-care-001 失败样本）。

## 事故复盘
评测场景 qa-care-001（"玫瑰怎么养得久？"）：
模型**调对了工具**（retrieve_knowledge 成功返回资料），但最终回复只有一句
"我已经为你整理好相关结果啦，请查看下方卡片～" —— 没有卡片、没有内容。

## 根因（v1 时代）
旧代码 `run()` 出口的 qa 空回复兜底条件是 `if route and route.name == 'qa'`，
而 v1 生产默认路由开关关闭 → `route` 恒为 None → **兜底永不触发**。

## v2 契约（2026-10-14 v1 退役后）
兜底判据不再依赖关键词路由，双通道：
1. 理解层声明 `qa` 意图（route.name == 'qa'）；或
2. **工具证据**：本轮有成功的 retrieve_knowledge（理解层降级时也生效）。
文本 UI + 无任何工具结果时，不得声称"结果在卡片里"（没有卡片可指）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_run_e2e import _install, _run, _chunk, _tc, _terminal  # noqa: E402

KNOWLEDGE_RESULT = json.dumps({
    'results': [{'name': '玫瑰', 'tips': ['45度斜剪根部2-3厘米', '每天换水并清洗花瓶', '避免阳光直射和暖气旁']}]
}, ensure_ascii=False)

QA_MESSAGE = '玫瑰怎么养得久？只讲养护，不要方案和图片。'
EMPTY_CARD_REPLY = '我已经为你整理好相关结果啦，请查看下方卡片～'


# ── 场景 1：理解层降级（intent=None），靠工具证据兜底 ──

def test_qa_empty_shell_reply_rescued_by_tool_evidence(monkeypatch):
    """retrieve_knowledge 成功 + 最终回复是空壳模板 → 必须重建实质回复。

    理解层降级（route=None）时不再有意图信号，兜底判据是本轮的成功检索证据。
    """
    turns = [
        [_chunk(tool_calls=[_tc(0, id='c1', name='retrieve_knowledge',
                                args='{"query": "玫瑰 养护"}')])],
        _terminal('respond_to_user',
                  json.dumps({'reply': EMPTY_CARD_REPLY, 'ui': 'text', 'data': {},
                              'stage': 'analyze', 'intent': 'qa'}, ensure_ascii=False), 'c2'),
    ]
    _install(monkeypatch, turns, tool_results={'retrieve_knowledge': KNOWLEDGE_RESULT})
    resp, _ = _run(QA_MESSAGE)
    reply = resp.reply or ''
    assert '查看下方卡片' not in reply, f'空壳模板句不应出现在 QA 回复中，实得：{reply}'
    assert '斜剪' in reply or '换水' in reply, f'回复应含检索资料里的实质建议，实得：{reply}'


# ── 场景 2：理解层声明 qa 意图（守门测试）──

def test_qa_empty_shell_reply_rescued_with_declared_intent(monkeypatch):
    """理解层声明 intent=qa 时兜底同样生效（不回归旧已修路径）。"""
    turns = [
        [_chunk(tool_calls=[_tc(0, id='c1', name='retrieve_knowledge',
                                args='{"query": "玫瑰 养护"}')])],
        _terminal('respond_to_user',
                  json.dumps({'reply': EMPTY_CARD_REPLY, 'ui': 'text', 'data': {},
                              'stage': 'analyze', 'intent': 'qa'}, ensure_ascii=False), 'c2'),
    ]
    _install(monkeypatch, turns, tool_results={'retrieve_knowledge': KNOWLEDGE_RESULT},
             intent='qa')
    resp, _ = _run(QA_MESSAGE)
    reply = resp.reply or ''
    assert '查看下方卡片' not in reply, f'intent=qa 时同样不得输出空壳句，实得：{reply}'
    assert '斜剪' in reply or '换水' in reply


# ── 场景 3：无工具结果时不得假装"结果在卡片里" ──

def test_no_tools_no_false_card_claim(monkeypatch):
    """TEXT UI 且没有任何工具结果时，回复不得声称"结果在卡片里"（根本没有卡片）。"""
    # 模型直接回空 reply（respond_to_user 参数里 reply 缺失/空串）
    turns = [_terminal('respond_to_user',
                       json.dumps({'reply': '', 'ui': 'text', 'data': {},
                                   'stage': 'analyze', 'intent': 'qa'}, ensure_ascii=False), 'c1')]
    _install(monkeypatch, turns, tool_results={})
    resp, _ = _run(QA_MESSAGE)
    reply = resp.reply or ''
    assert '查看下方卡片' not in reply, f'无卡片可指时不得声称卡片，实得：{reply}'
    assert reply.strip(), '兜底后回复不得为空'
