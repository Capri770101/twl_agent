import copy
import json
from types import SimpleNamespace

import agent.tools as tools
import pytest


def original():
    return {'plan_id': 'DIY_original', 'version': 1, 'diy': True, 'budget_num': 200, 'price': 200,
            'effect_image_url': '/generated/old.png', 'design': {
                'main_flowers': [{'name': '康乃馨', 'qty': 15}, {'name': '洋桔梗', 'qty': 15}],
                'fillers': [], 'foliage': [], 'color_scheme': ['粉', '白'], 'packaging': '白色纸包装'}}


def response(patch):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'changes': patch})))])


def test_color_then_packaging_preserves_materials_and_price(monkeypatch):
    old = original()
    snapshot = copy.deepcopy(old)
    monkeypatch.setattr(tools, 'call_llm', lambda *a, **kw: response({'color_scheme': ['香槟', '白']}))
    new = tools.revise_with_llm(json.dumps(old), '把配色改成香槟白，保留预算和花材')
    assert new['design']['main_flowers'] == old['design']['main_flowers']
    assert new['price'] == new['budget_num'] == 200
    assert 'effect_image_url' not in new
    assert '洋桔梗 15' in new['effect_prompt']
    monkeypatch.setattr(tools, 'call_llm', lambda *a, **kw: response({'packaging_color': '粉色'}))
    third = tools.revise_with_llm(json.dumps(new), '再次修改方案，改成粉色包装，但这次不要生成效果图，只给方案')
    assert third['version'] == 3
    assert third['parent_id'] == new['plan_id']
    assert third['design']['color_scheme'] == ['香槟', '白']
    assert third['design']['main_flowers'] == old['design']['main_flowers']
    assert third['price'] == 200
    assert old == snapshot


def test_preservation_clause_does_not_hide_color_change(monkeypatch):
    monkeypatch.setattr(tools, 'call_llm', lambda *a, **kw: response({'color_scheme': ['白']}))
    new = tools.revise_with_llm(json.dumps(original()), '保留预算和花材只改配色为白色')
    assert new['design']['color_scheme'] == ['白']
    assert new['design']['main_flowers'] == original()['design']['main_flowers']


def test_packaging_material_change_requires_estimate():
    result = tools.revise_with_llm(json.dumps(original()), '包装换成花篮')
    assert result['ok'] is False
    assert '重新估价' in result['error']


def test_packaging_color_cannot_replace_material(monkeypatch):
    monkeypatch.setattr(tools, 'call_llm', lambda *a, **kw: response({'packaging_color': '粉色丝绒礼盒'}))
    assert tools.revise_with_llm(json.dumps(original()), '包装改成粉色')['ok'] is False


def test_quantity_is_not_budget():
    assert 'budget' not in tools._extract_feedback('主花改成11支')['dims']
    assert 'budget' not in tools._extract_feedback('便宜点')['dims']
    assert tools._extract_feedback('预算调整到180元')['dims']['budget'] == '180'
    assert tools._extract_feedback('预算改成300')['dims']['budget'] == '300'


def test_vague_budget_requires_clarification_without_llm(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('must not silently redesign')
    monkeypatch.setattr(tools, 'call_llm', forbidden)
    result = tools.revise_with_llm(json.dumps(original()), '便宜点，花材保持不变')
    assert result['ok'] is False
    assert '预算金额' in result['error']


def test_unauthorized_patch_fails_without_rebuilding(monkeypatch):
    monkeypatch.setattr(tools, 'call_llm', lambda *a, **kw: response({'color_scheme': ['白'], 'main_flowers': []}))
    monkeypatch.setattr(tools, '_build_plan', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('rebuilt')))
    assert tools.revise_with_llm(json.dumps(original()), '配色改成白色')['ok'] is False


def test_timeout_keeps_original(monkeypatch):
    def timeout(*a, **kw):
        raise TimeoutError()
    monkeypatch.setattr(tools, 'call_llm', timeout)
    assert tools.revise_with_llm(json.dumps(original()), '包装改成粉色')['ok'] is False


def test_tool_uses_session_latest_and_does_not_store_failure(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    import agent.diy_tools as diy
    from backend.storage import memory
    latest = original()
    latest['version'] = 4
    monkeypatch.setattr(memory, 'get_session_json', AsyncMock(return_value=latest))
    store = AsyncMock()
    monkeypatch.setattr(diy, '_store_diy_plan', store)
    def revise(plan, feedback, **kwargs):
        assert json.loads(plan)['version'] == 4
        return {'ok': False, 'error': '模拟失败'}
    monkeypatch.setattr(tools, 'revise_with_llm', revise)
    result = asyncio.run(diy.revise_diy_plan(json.dumps(original()), '改配色', {'user_id': 'u', 'session_id': 's'}))
    assert json.loads(result)['ok'] is False
    store.assert_not_awaited()


@pytest.mark.parametrize('feedback,flowers,price,error', [
    ('洋桔梗换成玫瑰，总支数不变', [('康乃馨', 15), ('玫瑰', 15)], 180, None),
    ('洋桔梗换成玫瑰，总支数不变', [('康乃馨', 15), ('玫瑰', 16)], 180, '总支数'),
    ('预算调整到180元，保留全部花材', [('康乃馨', 15), ('玫瑰', 15)], 170, '花材'),
    ('预算调整到180元，保留全部花材', [('康乃馨', 12), ('洋桔梗', 12)], 170, None),
    ('预算调整到180元，保留全部花材', [('康乃馨', 15), ('洋桔梗', 15)], 200, '超过预算'),
    ('预算调整到180元，每种花的支数不变', [('康乃馨', 14), ('洋桔梗', 16)], 170, '原支数'),
])
def test_final_revision_constraints(monkeypatch, feedback, flowers, price, error):
    # 隔离上游语义生成/定价，验证最终候选仍必须经过约束检查。
    import agent.plan_validator as validator
    monkeypatch.setattr(tools, '_retrieve_for_design', lambda *a: '')
    monkeypatch.setattr(tools, 'call_llm', lambda *a, **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"design": {}}'))]))
    monkeypatch.setattr(validator, 'repair_plan', lambda p: p)
    def merge(baseline, *a, **kw):
        assert baseline['design']['main_flowers'] == original()['design']['main_flowers']
        assert 'effect_image_url' not in baseline
        candidate = copy.deepcopy(baseline)
        candidate['design']['main_flowers'] = [{'name': n, 'qty': q} for n, q in flowers]
        candidate['price'] = price
        return candidate
    monkeypatch.setattr(tools, '_merge_plan', merge)
    result = tools.revise_with_llm(json.dumps(original()), feedback)
    if error:
        assert result['ok'] is False
        assert error in result['error']
    else:
        assert result['parent_id'] == 'DIY_original'
        assert result['version'] == 2
        assert result['price'] == price


def test_visual_revision_uses_real_plan_and_keeps_quote(monkeypatch):
    # 不替换合并/费用函数：真实规则方案经过两次局部修订后仍逐字段保留用料和费用。
    old = tools._build_plan({'recipient': '母亲', 'occasion': '生日', 'budget': '200'})
    old['copy_text'] = tools.build_plan_copy_text(old)
    monkeypatch.setattr(tools, 'call_llm', lambda *a, **kw: response({'color_scheme': ['香槟', '白']}))
    second = tools.revise_with_llm(json.dumps(old), '配色改成香槟白，保留预算和花材')
    monkeypatch.setattr(tools, 'call_llm', lambda *a, **kw: response({'packaging_color': '粉色'}))
    third = tools.revise_with_llm(json.dumps(second), '包装改成粉色，总支数不变')
    for key in ('main_flowers', 'fillers', 'foliage', 'fees'):
        assert third['design'][key] == old['design'][key]
    for key in ('price', 'budget_num', 'budget_breakdown'):
        assert third[key] == old[key]
    assert third['version'] == 3
    assert third['parent_id'] == second['plan_id']
