from scripts.run_eval import judge, tool_names


def test_tool_names_accepts_public_contract_forms():
    assert tool_names({'tool_calls': ['a', {'name': 'b'}, {'x': 1}]}) == ['a', 'b']


def test_judge_detects_ui_and_forbidden_tools():
    case = {'expected_ui': 'text', 'allowed_tools': ['retrieve_knowledge'], 'forbidden_tools': ['generate_effect_image']}
    passed, issues = judge(case, {'ui': 'plan_card', 'tool_calls': [{'name': 'generate_effect_image'}]})
    assert not passed
    assert any('expected' in issue for issue in issues)
    assert any('forbidden' in issue for issue in issues)


def test_judge_passes_expected_response():
    case = {'expected_ui': 'text', 'allowed_tools': ['retrieve_knowledge', 'respond_to_user'], 'forbidden_tools': ['generate_diy_plan']}
    assert judge(case, {'ui': 'text', 'tool_calls': [{'name': 'retrieve_knowledge'}]}) == (True, [])
