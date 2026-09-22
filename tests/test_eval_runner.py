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
    assert judge(case, {'ui': 'text', 'reply': '水变浑浊时及时换水并清洗花瓶。', 'tool_calls': [{'name': 'retrieve_knowledge'}]}) == (True, [])


def test_qa_placeholder_is_not_a_pass():
    passed, issues = judge({'intent': 'qa', 'expected_ui': 'text'}, {'ui': 'text', 'reply': '我已经为你整理好相关结果啦，请查看下方卡片～'})
    assert not passed
    assert any('substantive' in issue for issue in issues)
