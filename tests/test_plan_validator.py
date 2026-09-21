from types import SimpleNamespace

from agent.plan_validator import validate_plan, annotate_plan_validation


def plan(**design):
    return {'name': 'x', 'budget_num': 150, 'design': design}


def test_valid_plan():
    assert validate_plan(plan(main_flowers=[{'name': '玫瑰', 'qty': 11}])) == []


def test_single_flower_and_stem_count():
    req = SimpleNamespace(single_flower='玫瑰', stem_count=11, budget_num=200, excluded_flowers=[])
    errors = validate_plan(plan(main_flowers=[{'name': '玫瑰', 'qty': 10}], fillers=[{'name': '满天星', 'qty': 1}]), req)
    assert any('单一花材' in x for x in errors)
    assert any('支数' in x for x in errors)


def test_excluded_and_budget():
    req = SimpleNamespace(single_flower=None, stem_count=None, budget_num=100, excluded_flowers=['百合'])
    errors = validate_plan(plan(main_flowers=[{'name': '百合', 'qty': 3}]), req)
    assert any('排除' in x for x in errors)
    assert any('预算' in x for x in errors)


def test_annotation_is_structured():
    out = annotate_plan_validation(plan(main_flowers=[{'name': '百合', 'qty': 3}]), SimpleNamespace(excluded_flowers=['百合']))
    assert out['validation_status'] == 'needs_review'
    assert out['validation_errors']
