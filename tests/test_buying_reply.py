import json
from types import SimpleNamespace

from agent.safety.buying_reply import missing_product_reply
from agent.safety.card_claim import _needs_card_nudge


def _row(entity='plan', data=None, ok=True):
    return SimpleNamespace(name='platform_db_query_entity', arguments={'entity': entity}, status='ok',
                           result=json.dumps({'ok': ok, 'data': data}))


def test_failed_product_query_never_points_to_a_nonexistent_card():
    reply = missing_product_reply([_row(ok=False), _row(data=[])], '买现成的送妈妈，表达感谢')
    assert '没查到' in reply and '库存' in reply
    assert '表达感谢' in reply
    assert '请查看下方卡片' not in reply
    assert '玫瑰' not in reply


def test_successful_query_or_unrelated_tool_does_not_replace_reply():
    assert missing_product_reply([_row(data=[{'name': '真实花束'}])]) is None
    assert missing_product_reply([_row(entity='shop', ok=False)]) is None


def test_explicit_off_the_shelf_request_does_not_trigger_diy_card_fallback():
    assert not _needs_card_nudge('预算200买现成的送妈妈', [], True, '送妈妈一束粉色花束，约199元')
