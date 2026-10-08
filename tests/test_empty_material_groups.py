from agent.tools import _build_plan, _merge_plan


def test_explicit_empty_groups_clear_baseline_materials():
    baseline = _build_plan({'recipient':'母亲','occasion':'生日','budget':'200'})
    result = _merge_plan(baseline, {'design': {'main_flowers': [
        {'name':'康乃馨','qty':13}, {'name':'洋桔梗','qty':12}], 'fillers': [], 'foliage': []}})
    assert result['design']['fillers'] == []
    assert result['design']['foliage'] == []
    assert '勿忘我' not in result['effect_prompt']
    assert '银叶菊' not in result['effect_prompt']
