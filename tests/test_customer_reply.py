import json
from types import SimpleNamespace

from agent.safety.customer_reply import align_after_sales_reply, cs_claim_without_evidence


def _record(order_id='TLOCAL2026100807', amount=15900, state='pending_review', ok=True):
    return SimpleNamespace(name='query_my_after_sales', status='ok', result=json.dumps({
        'ok': ok, 'data': {'id': order_id, 'after_sales': {'state': state, 'amount_cents': amount}},
    }))


def test_after_sales_repairs_wrong_number_and_omitted_amount():
    reply = '订单 T202610007 的退款目前还在商家审核中，尚未到账。'
    aligned = align_after_sales_reply(reply, [_record()])
    assert 'TLOCAL2026100807' in aligned and '159 元' in aligned
    assert 'T202610007' not in aligned and '尚未' in aligned


def test_after_sales_keeps_good_explanation_and_adds_missing_amount():
    reply = '订单 TLOCAL2026100807 目前还在审核中，可以等商家的进度。'
    aligned = align_after_sales_reply(reply, [_record()])
    assert aligned.startswith(reply) and '159 元' in aligned


def test_after_sales_does_not_claim_success_on_failed_or_ambiguous_evidence():
    reply = '暂时查不到订单。'
    assert align_after_sales_reply(reply, [_record(ok=False)]) == reply
    assert align_after_sales_reply(reply, [_record(), _record(order_id='TLOCAL2026100811')]) == reply


def test_cs_claim_without_evidence_flags_zero_tool_statements():
    # 无工具 + 状态断言/订单号 → 拦
    assert cs_claim_without_evidence('你的订单还在审核中。', [])
    assert cs_claim_without_evidence('TLOCAL2026100811 已退款到账。', [])
    assert cs_claim_without_evidence('这笔 50 元的退款配送中。', [])
    # 澄清提问、登录引导（零工具但无状态断言）→ 放行
    assert not cs_claim_without_evidence('你想查哪一笔订单？告诉我订单号就行。', [])
    assert not cs_claim_without_evidence('查订单需要先登录你的账号哦。', [])
    assert not cs_claim_without_evidence('', [])
    # 有成功客服工具 → 放行
    ok_tool = SimpleNamespace(name='query_my_orders', status='ok')
    assert not cs_claim_without_evidence('你的订单在配送中。', [ok_tool])
    # 失败的工具不算证据
    err_tool = SimpleNamespace(name='query_my_orders', status='error')
    assert cs_claim_without_evidence('你的订单在配送中。', [err_tool])
