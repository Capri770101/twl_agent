"""run_eval.judge 的结构化断言单元测试（不依赖运行中的服务）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_eval import evaluate_response, judge  # noqa: E402


def _base_case() -> dict:
    return {
        'id': 'unit-001', 'message': '测试', 'intent': 'qa',
        'expected_ui': 'text', 'allowed_tools': ['retrieve_knowledge', 'respond_to_user'],
        'forbidden_tools': ['generate_diy_plan'], 'max_iterations': 2,
        'checks': [],
    }


def _response(reply='好的', ui='text', tools=()) -> dict:
    return {'reply': reply, 'ui': ui, 'tool_calls': list(tools)}


def test_string_only_checks_require_review():
    case = _base_case()
    case['checks'] = ['人工判定项']
    passed, issues = judge(case, _response())
    assert passed and not issues
    result = evaluate_response(case, _response())
    assert result['machine_passed'] is True
    assert result['manual_checks'] == 1
    assert result['manual_check_items'] == ['人工判定项']
    assert result['status'] == 'needs_review'
    assert result['passed'] is False


def test_fully_automated_case_can_pass():
    case = _base_case()
    case['checks'] = [{'type': 'tool_used', 'value': 'retrieve_knowledge'}]
    result = evaluate_response(case, _response(tools=['retrieve_knowledge']))
    assert result['status'] == 'passed' and result['passed'] is True
    assert result['manual_checks'] == 0


def test_failed_machine_check_stays_failed_with_manual_checks():
    case = _base_case()
    case['checks'] = ['人工检查', {'type': 'tool_used', 'value': 'retrieve_knowledge'}]
    result = evaluate_response(case, _response())
    assert result['status'] == 'failed' and result['passed'] is False


def test_must_include_pass_and_fail():
    case = _base_case()
    case['checks'] = [{'type': 'must_include', 'value': '换水'}]
    passed, _ = judge(case, _response(reply='每两天换水一次'))
    assert passed
    passed, issues = judge(case, _response(reply='直接扔水里'))
    assert not passed and any('must_include' in i for i in issues)


def test_must_not_include():
    case = _base_case()
    case['checks'] = [{'type': 'must_not_include', 'value': '取消成功'}]
    passed, _ = judge(case, _response(reply='该渠道暂未打通'))
    assert passed
    passed, issues = judge(case, _response(reply='已为你取消成功'))
    assert not passed and any('must_not_include' in i for i in issues)


def test_tool_used_and_not_used():
    case = _base_case()
    case['checks'] = [{'type': 'tool_used', 'value': 'retrieve_knowledge'},
                      {'type': 'tool_not_used', 'value': 'generate_diy_plan'}]
    passed, _ = judge(case, _response(tools=['retrieve_knowledge']))
    assert passed
    _, issues = judge(case, _response(tools=['retrieve_knowledge', 'generate_diy_plan']))
    assert any('tool_not_used' in i for i in issues)
    _, issues = judge(case, _response(tools=[]))
    assert any('tool_used' in i for i in issues)


def test_ui_is_assertion():
    case = _base_case()
    case['checks'] = [{'type': 'ui_is', 'value': 'text'}]
    passed, _ = judge(case, _response(ui='text'))
    assert passed
    _, issues = judge(case, _response(ui='plan_card'))
    assert any('ui_is' in i for i in issues)


def test_mixed_string_and_object_checks():
    case = _base_case()
    case['checks'] = ['人工项', {'type': 'tool_used', 'value': 'retrieve_knowledge'}]
    passed, _ = judge(case, _response(tools=['retrieve_knowledge']))
    assert passed


def test_unknown_assertion_type_fails_closed():
    case = _base_case()
    case['checks'] = [{'type': 'made_up', 'value': 'x'}]
    passed, issues = judge(case, _response())
    assert not passed and any('unknown check assertion' in issue for issue in issues)
