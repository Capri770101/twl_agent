"""交易声称护栏：无交易工具证据时不得声称下单/支付完成。"""
import json
from types import SimpleNamespace

from agent.safety.trade_claim import (
    _claims_trade_done, _finalize_trade_claim, _needs_trade_nudge,
)


def _order_call(ok=True):
    result = json.dumps({'order_id': 'T1'}) if ok else json.dumps({'error': 'x'})
    return SimpleNamespace(name='create_order', status='ok' if ok else 'error', result=result)


def test_claims_trade_done_matches_only_completed_claims():
    assert _claims_trade_done('已经下单成功，价格 88 元')
    assert _claims_trade_done('已为你下单，预计明天送达')
    assert _claims_trade_done('支付成功！')
    assert _claims_trade_done('订单已生成，请查收')
    assert not _claims_trade_done('可以点卡片去下单')
    assert not _claims_trade_done('购买请在小程序的商品页完成下单')
    assert not _claims_trade_done('这笔订单待支付，请尽快下单')
    assert not _claims_trade_done('本系统不能下单，请在小程序完成')
    assert not _claims_trade_done('尚未下单，需要我先帮你查在售商品吗？')
    assert not _claims_trade_done('')


def test_nudge_requires_trade_tool_evidence():
    assert _needs_trade_nudge([], '已经下单成功')
    assert not _needs_trade_nudge([_order_call()], '已经下单成功')


def test_finalize_replaces_lingering_false_claim():
    assert _finalize_trade_claim('已经下单成功', []) != '已经下单成功'
    assert _finalize_trade_claim('好的，收到', []) == '好的，收到'
