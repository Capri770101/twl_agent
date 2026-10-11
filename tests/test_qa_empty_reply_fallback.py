"""QA 空壳回复兜底回归（2026-10-08，源自 2026-09-22 真实模型评测 qa-care-001 失败样本）。

## 事故复盘
评测场景 qa-care-001（"玫瑰怎么养得久？"）：
模型**调对了工具**（retrieve_knowledge 成功返回资料），但最终回复只有一句
"我已经为你整理好相关结果啦，请查看下方卡片～" —— 没有卡片、没有内容。

## 根因
旧代码 `run()` 出口的 qa 空回复兜底条件是 `if route and route.name == 'qa'`，
而 `route` 来自 `classify(message) if settings.AGENT_INTENT_ROUTING_ENABLED else None`。
生产默认 `AGENT_INTENT_ROUTING_ENABLED=False` → `route` 恒为 None → **兜底永不触发**。
单测没抓住它，因为相关测试都开着开关跑。

## 本文件锁定两个契约
1. **路由开关关闭**（生产默认）时：qa 消息 + 空壳/空回复 → 必须用
   `knowledge_fallback_reply` 从检索结果重建实质回复。
2. 文本 UI + 无任何工具结果时，不得声称"结果在卡片里"（没有卡片可指）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from tests.test_run_e2e import _install, _run, _chunk, _tc, _terminal  # noqa: E402

KNOWLEDGE_RESULT = json.dumps({
    'results': [{'name': '玫瑰', 'tips': ['45度斜剪根部2-3厘米', '每天换水并清洗花瓶', '避免阳光直射和暖气旁']}]
}, ensure_ascii=False)

QA_MESSAGE = '玫瑰怎么养得久？只讲养护，不要方案和图片。'
EMPTY_CARD_REPLY = '我已经为你整理好相关结果啦，请查看下方卡片～'


# ── 场景 1：路由开关关闭（生产默认），模型调对工具但回空壳模板 ──

def test_qa_empty_shell_reply_rescued_with_routing_disabled(monkeypatch):
    """QA 消息 + retrieve_knowledge 成功 + 最终回复是空壳模板 → 必须重建实质回复。

    这是 2026-09-22 评测 qa-care-001 的精确复现条件：路由关闭（route=None）。
    修复前该路径下兜底条件永假，用户只能看到空壳句。
    """
    monkeypatch.setattr(settings, 'AGENT_INTENT_ROUTING_ENABLED', False)
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


# ── 场景 2：开关打开时，原有兜底路径不受影响（守门测试）──

def test_qa_empty_shell_reply_rescued_with_routing_enabled(monkeypatch):
    """AGENT_INTENT_ROUTING_ENABLED=True 时兜底同样生效（不回归旧已修路径）。"""
    monkeypatch.setattr(settings, 'AGENT_INTENT_ROUTING_ENABLED', True)
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
    assert '查看下方卡片' not in reply, f'开关开启时同样不得输出空壳句，实得：{reply}'
    assert '斜剪' in reply or '换水' in reply


# ── 场景 3：无工具结果时不得假装"结果在卡片里" ──

def test_no_tools_no_false_card_claim(monkeypatch):
    """TEXT UI 且没有任何工具结果时，回复不得声称"结果在卡片里"（根本没有卡片）。"""
    monkeypatch.setattr(settings, 'AGENT_INTENT_ROUTING_ENABLED', False)
    # 模型直接回空 reply（respond_to_user 参数里 reply 缺失/空串）
    turns = [_terminal('respond_to_user',
                       json.dumps({'reply': '', 'ui': 'text', 'data': {},
                                   'stage': 'analyze', 'intent': 'qa'}, ensure_ascii=False), 'c1')]
    _install(monkeypatch, turns, tool_results={})
    resp, _ = _run(QA_MESSAGE)
    reply = resp.reply or ''
    assert '查看下方卡片' not in reply, f'无卡片可指时不得声称卡片，实得：{reply}'
    assert reply.strip(), '兜底后回复不得为空'
