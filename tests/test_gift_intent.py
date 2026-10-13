from agent.design.gift_intent import rank_products_for_gift
from agent.design.requirement import extract_requirement
from agent.tools import _build_plan
from domain.requirements import FlowerRequirement, accumulate


def test_message_to_recipient_is_not_bouquet_mood():
    req = extract_requirement('送妈妈一束花，想告诉她谢谢这些年的照顾，预算200')
    assert req.recipient == '母亲' and req.budget_num == 200
    assert req.gift_intent == 'gratitude' and req.gift_message == '谢谢这些年的照顾'
    assert req.mood is None
    assert FlowerRequirement.from_dict(req.to_dict()).gift_intent == 'gratitude'


def test_apology_does_not_mean_romance_or_reconciliation():
    req = extract_requirement('我想道歉，但不想复合；不要玫瑰，只想表达我确实做错了')
    assert req.gift_intent == 'apology'
    assert req.gift_intent != 'affection'


def test_correction_and_across_turn_retraction():
    req = extract_requirement('不是送女友，是送妈妈，想谢谢她的照顾')
    assert req.recipient == '母亲' and req.gift_intent == 'gratitude'
    prior = extract_requirement('想跟女友道歉')
    changed = accumulate(prior, extract_requirement('不是要道歉，只是问下预算'))
    assert changed.gift_intent is None
    assert accumulate(prior, extract_requirement('预算300')).gift_intent == 'apology'


def test_diy_gift_fit_uses_real_flower_language_without_overriding_explicit_flower():
    dims = extract_requirement('给妈妈定制一束感谢她的花，预算200').to_legacy_dict()
    plan = _build_plan(dims)
    assert plan['gift_intent'] == 'gratitude'
    assert plan['recipient'] == '母亲'
    assert any(set(f['flower_language']) & {'感恩', '感激', '尊敬', '温馨', '用心'}
               for f in plan['design']['main_flowers'])
    specified = _build_plan({**dims, 'single_flower': '玫瑰', 'stem_count': '11'})
    assert [f['name'] for f in specified['design']['main_flowers']] == ['玫瑰']
    assert specified['stem_count'] == 11


def test_product_ranking_requires_real_material_evidence():
    rows = [{'name': '随机礼包', 'price': 50}, {'name': '康乃馨花束', 'price': 180},
            {'name': '玫瑰花束', 'price': 190}]
    ranked = rank_products_for_gift(rows, 'gratitude')
    assert ranked[0] is rows[1]
    assert ranked[1:] == [rows[2], rows[0]]  # 玫瑰有“尊敬”，未知花材仍靠后
    assert rank_products_for_gift(rows, None) == rows
    priced = [{'name': '康乃馨花束', 'price_yuan': 300}, {'name': '玫瑰花束', 'price_yuan': 180}]
    assert rank_products_for_gift(priced, 'gratitude', budget=200)[0] is priced[1]


def test_platform_product_result_is_ranked_but_never_invented(monkeypatch):
    products = [{'name': '玫瑰', 'price_yuan': 180}, {'name': '康乃馨', 'price_yuan': 190}]
    monkeypatch.setattr(data_tools, 'query_external_entity', lambda *args, **kwargs: list(products))
    result = json.loads(data_tools.platform_db_query_entity(
        'test', 'plan', _context={'user_message': '预算200，买现成花送妈妈，想表达感谢'}))
    assert result['ok'] is True
    assert result['data'] == [products[1], products[0]]
    assert all('flower_language' not in product for product in result['data'])


def test_gift_message_only_uses_the_users_own_words(monkeypatch):
    from agent import tools
    original = extract_requirement('给妈妈定制一束花，想告诉她谢谢这些年的照顾，预算200')
    monkeypatch.setattr(tools, '_retrieve_for_design', lambda _: '')
    monkeypatch.setattr(tools, 'call_llm', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('offline')))
    plan = tools.design_with_llm('想告诉她：她谢谢这些年的照顾、她已经原谅我', session_requirement=original)
    assert plan['gift_message'] == '谢谢这些年的照顾'
    assert '她谢谢' not in plan['card_message']


def test_model_cannot_invent_card_promise_after_apology():
    from agent.tools import _merge_plan
    baseline = _build_plan(extract_requirement('想向女朋友道歉，但不想逼她复合，预算300').to_legacy_dict())
    merged = _merge_plan(baseline, {'design': {'card_message': '你一定会原谅我，我们马上复合。'}})
    assert merged['card_message'] == baseline['card_message']
    assert '复合' not in merged['card_message']
import json

from agent import data_tools
