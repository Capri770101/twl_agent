from types import SimpleNamespace

from agent.plan_validator import validate_plan, validate_plan_warnings, annotate_plan_validation, repair_plan


def plan(**design):
    return {'name': 'x', 'budget_num': 150, 'design': design}


def test_valid_plan():
    assert validate_plan(plan(main_flowers=[{'name': '玫瑰', 'qty': 11}])) == []


def test_single_flower_and_stem_count_are_warnings_not_blocking():
    req = SimpleNamespace(single_flower='玫瑰', stem_count=11, budget_num=200, excluded_flowers=[])
    warnings = validate_plan_warnings(plan(main_flowers=[{'name': '玫瑰', 'qty': 10}], fillers=[{'name': '满天星', 'qty': 1}]), req)
    assert any('单一花材' in x for x in warnings)
    assert any('支数' in x for x in warnings)
    assert validate_plan(plan(main_flowers=[{'name': '玫瑰', 'qty': 10}], fillers=[{'name': '满天星', 'qty': 1}]), req) == []


def test_excluded_is_blocking_but_budget_is_warning():
    req = SimpleNamespace(single_flower=None, stem_count=None, budget_num=100, excluded_flowers=['百合'])
    errors = validate_plan(plan(main_flowers=[{'name': '百合', 'qty': 3}]), req)
    assert any('排除' in x for x in errors)
    warnings = validate_plan_warnings(plan(main_flowers=[{'name': '百合', 'qty': 3}]), req)
    assert any('预算' in x for x in warnings)


def test_annotation_is_structured():
    out = annotate_plan_validation(plan(main_flowers=[{'name': '百合', 'qty': 3}]), SimpleNamespace(excluded_flowers=['百合']))
    assert out['validation_status'] == 'needs_review'
    assert out['validation_errors']


def test_repair_exclusion_single_flower_and_count():
    req = SimpleNamespace(single_flower='玫瑰', stem_count=11, budget_num=200, excluded_flowers=['百合'])
    original = plan(main_flowers=[{'name': '玫瑰', 'qty': 5}, {'name': '百合', 'qty': 3}], fillers=[{'name': '满天星', 'qty': 2}], foliage=[{'name': '尤加利', 'qty': 1}])
    out = repair_plan(original, req)
    assert out['design']['main_flowers'] == [{'name': '玫瑰', 'qty': 11}]
    assert out['design']['fillers'] == [] and out['design']['foliage'] == []
    assert out['validation_status'] == 'ok'


def test_over_budget_is_warning_not_blocked():
    req = SimpleNamespace(single_flower=None, stem_count=None, budget_num=100, excluded_flowers=[])
    out = repair_plan(plan(main_flowers=[{'name': '玫瑰', 'qty': 11}]), req)
    assert out['validation_status'] == 'has_warnings'
    assert 'validation_errors' not in out
    assert any('预算' in w for w in out['validation_warnings'])
