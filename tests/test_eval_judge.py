"""run_eval.judge 的结构化断言单元测试（不依赖运行中的服务）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_eval import judge  # noqa: E402


def _base_case() -> dict:
    return {
        'id': 'unit-001', 'message': '测试', 'intent': 'qa',
        'expected_ui': 'text', 'allowed_tools': ['retrieve_knowledge', 'respond_to_user'],
        'forbidden_tools': ['generate_diy_plan'], 'max_iterations': 2,
        'checks': [],
    }


def _response(reply='好的', ui='text', tools=()) -> dict:
    return {'reply': reply, 'ui': ui, 'tool_calls': list(tools)}


def test_string_only_checks_are_manual_and_pass():
    case = _base_case()
    case['checks'] = ['人工判定项']
    passed, issues = judge(case, _response())
    assert passed and not issues


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


def test_unknown_assertion_type_is_ignored_not_crash():
    case = _base_case()
    case['checks'] = [{'type': 'made_up', 'value': 'x'}]
    passed, _ = judge(case, _response())
    # 未识别类型按人工项跳过（validate_eval_set 负责格式门禁）
    assert passed
