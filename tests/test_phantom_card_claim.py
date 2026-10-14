"""幻影卡片声称护栏：回复引用本轮不存在的卡片 → 拦截重答；重答仍犯则删除分句。"""
from __future__ import annotations

from types import SimpleNamespace

from agent.engine.cleanup import build_cleanup_pipeline, run_cleanup_pipeline
from agent.safety.card_claim import _finalize_phantom_card_claim, _phantom_card_claim
from tests.test_run_e2e import _install, _run, _terminal


def _card_tool():
    return SimpleNamespace(name='generate_diy_plan', status='ok',
                           result='{"plan_id": "DIY_x", "diy": true}', arguments={})


def test_phantom_claim_needs_no_card_evidence():
    assert _phantom_card_claim('【该卡片已失效，请忽略】', [])
    assert _phantom_card_claim('请查看下方卡片', [])
    assert _phantom_card_claim('这张卡片里有明细', [])
    # 有真实卡片产出 → 放行
    assert not _phantom_card_claim('细节都在卡片里', [_card_tool()])
    # 不引用卡片的正常回复 → 放行
    assert not _phantom_card_claim('好的，马上给你配一束', [])
    assert not _phantom_card_claim('', [])


def test_finalize_strips_phantom_clause_without_card():
    class _UI:
        value = 'text'
    out = _finalize_phantom_card_claim('请查看下方卡片。另外也可以直接告诉我预算。', ui=_UI(), data={})
    assert '卡片' not in out and '预算' in out
    # 整段都是幻影声称 → 诚实引导
    only = _finalize_phantom_card_claim('【该卡片已失效，请忽略】', ui=_UI(), data={})
    assert '已失效' not in only and only.strip()


def test_finalize_keeps_claim_when_card_exists():
    class _UI:
        value = 'plan_card'
    reply = '请查看下方卡片'
    assert _finalize_phantom_card_claim(reply, ui=_UI(), data={'plans': [{}]}) == reply


def test_phantom_card_claim_triggers_guard_retry(monkeypatch):
    """实测复现（2026-10-13）：模型零工具回「该卡片已失效，请忽略」→ 必须拦下重答。"""
    turns = [
        _terminal('respond_to_user',
                  '{"reply": "【该卡片已失效，请忽略】", "ui": "text", "data": {}}', 'c1'),
        _terminal('respond_to_user',
                  '{"reply": "你好，想定制花束还是看看现成的？跟我说说需求就行。", '
                  '"ui": "text", "data": {}, "intent": "other"}', 'c2'),
    ]
    fake = _install(monkeypatch, turns)
    resp, _ = _run('给妈妈定制一束感谢她的花，预算200，顺便出个效果图')
    assert fake.llm_calls['n'] >= 2, '幻影卡片声称必须触发一次重答'
    assert '已失效' not in resp.reply and '请忽略' not in resp.reply


def test_phantom_claim_surviving_retry_is_scrubbed_before_publish(monkeypatch):
    """重答后仍幻影声称 → 发布前由清理链确定性删除。"""
    pipeline = build_cleanup_pipeline(lambda r: r, lambda: True)
    class _UI:
        value = 'text'
    out = run_cleanup_pipeline('【该卡片已失效，请忽略】', pipeline=pipeline,
                               ui=_UI(), data={}, tool_log=[])
    assert '已失效' not in out  # 幻影卡片声称必须被移除
    assert out.strip(), '清空后必须有非空兜底'
