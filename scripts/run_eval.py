"""对已运行的智能体执行真实 HTTP 场景评测，不读取生产 .env。

示例（H5 同源代理会注入平台 Key）：
  python scripts/run_eval.py --base-url http://129.204.85.139/agent

直连智能体时由调用者在进程环境设置 EVAL_PLATFORM_API_KEY，勿把 Key 写进命令或报告。
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = ROOT / 'evals' / 'flower_scenarios.jsonl'
RESULTS = ROOT / 'evals' / 'results'


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def tool_names(response: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in response.get('tool_calls') or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get('name') and item.get('source', 'model') == 'model':
            names.append(str(item['name']))
    return names


#: checks 结构化断言的支持类型（judge 自动判定）。
#: 字符串条目保留为人工项；未经复核的场景不能计入已通过。
CHECK_ASSERTION_TYPES = frozenset({'must_include', 'must_not_include', 'ui_is', 'tool_used', 'tool_not_used'})


def _judge_check_assertions(case: dict[str, Any], response: dict[str, Any]) -> tuple[list[str], int]:
    """执行 checks 里的结构化断言；返回 (issues, 人工项数量)。"""
    issues: list[str] = []
    manual = 0
    reply = str(response.get('reply') or '')
    ui = str(response.get('ui') or '')
    used = set(tool_names(response))
    for check in case.get('checks') or []:
        if isinstance(check, str):
            manual += 1
            continue
        kind = str(check.get('type') or '')
        value = check.get('value')
        if kind == 'must_include':
            if str(value) not in reply:
                issues.append(f'check must_include: {value!r} missing in reply')
        elif kind == 'must_not_include':
            if str(value) in reply:
                issues.append(f'check must_not_include: {value!r} found in reply')
        elif kind == 'ui_is':
            if ui != str(value):
                issues.append(f'check ui_is: {ui!r} != {value!r}')
        elif kind == 'tool_used':
            if str(value) not in used:
                issues.append(f'check tool_used: {value!r} not called')
        elif kind == 'tool_not_used':
            if str(value) in used:
                issues.append(f'check tool_not_used: {value!r} was called')
        else:
            issues.append(f'unknown check assertion: {kind!r}')
    return issues, manual


def judge(case: dict[str, Any], response: dict[str, Any]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    reply = str(response.get('reply') or '').strip()
    if not reply:
        issues.append('empty reply')
    if str(case.get('intent')) == 'qa' and any(phrase in reply for phrase in ('请查看下方卡片', '服务暂时开小差', '思考得太久')):
        issues.append('qa did not provide a substantive answer')
    # 客服场景同样不容空壳：卡片类回复必须带基于数据的要点（2026-10-08 线上实测教训）
    if str(case.get('intent')) in ('shop_service', 'customer_service') and '请查看下方卡片' in reply:
        issues.append('customer service reply is an empty card shell')
    if response.get('ui') == 'plan_card' and not (response.get('data') or {}).get('plans'):
        issues.append('plan_card missing plans')
    ui = str(response.get('ui') or '')
    expected = str(case.get('expected_ui') or '')
    if expected and ui != expected:
        issues.append(f'ui={ui!r}, expected={expected!r}')
    used = tool_names(response)
    forbidden = set(case.get('forbidden_tools') or [])
    bad = sorted(forbidden.intersection(used))
    if bad:
        issues.append('forbidden tools: ' + ', '.join(bad))
    allowed = set(case.get('allowed_tools') or [])
    unexpected = sorted(set(used) - allowed) if allowed else []
    if unexpected:
        issues.append('unexpected tools: ' + ', '.join(unexpected))
    check_issues, _manual = _judge_check_assertions(case, response)
    issues.extend(check_issues)
    return (not issues, issues)


def evaluate_response(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """区分机器检查通过、需要人工复核及失败；人工项不冒充通过。"""
    machine_passed, issues = judge(case, response)
    manual_check_items = [check for check in case.get('checks') or [] if isinstance(check, str)]
    status = 'failed' if not machine_passed else 'needs_review' if manual_check_items else 'passed'
    return {
        'passed': status == 'passed', 'machine_passed': machine_passed,
        'manual_checks': len(manual_check_items), 'manual_check_items': manual_check_items,
        'status': status, 'issues': issues,
    }


def exchange_token(client: httpx.Client, base: str, external_id: str, api_key: str) -> dict[str, str]:
    headers = {'X-API-Key': api_key} if api_key else {}
    response = client.post(base + '/auth/token', headers=headers, json={'external_user_id': external_id})
    response.raise_for_status()
    data = response.json()
    return {'token': str(data['access_token']), 'user_id': str(data['user_id'])}


def customer_credential(client: httpx.Client, mock_base: str) -> str:
    """从 mock H5 业务后端换 cs1 票据（eval 客服场景用，仅指向本地 mock）。"""
    response = client.post(mock_base.rstrip('/') + '/credential')
    response.raise_for_status()
    return str((response.json().get('data') or {}).get('access_token') or '')


def customer_identity(client: httpx.Client, base: str, cs_token: str) -> dict[str, str]:
    """cs1 票据 → 客服命名空间的智能体 token（契约 §6：不能与平台 token 混用）。"""
    response = client.post(base + '/auth/customer-token', json={'credential': cs_token})
    response.raise_for_status()
    data = response.json()
    return {'token': str(data['access_token']), 'user_id': str(data['user_id'])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True, help='例如 http://127.0.0.1:8000 或 http://H5-IP/agent')
    parser.add_argument('--cases', type=Path, default=DEFAULT_CASES)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--insecure', action='store_true', help='仅 IP HTTPS 临时验收；优先使用可信代理/CA')
    parser.add_argument('--timeout', type=float, default=190.0)
    parser.add_argument('--customer-mock', type=str, default='',
                        help='本地 mock H5 业务后端基址（如 http://127.0.0.1:4761/api/customer-service）；'
                             '启用后带 customer_credential 标记的场景会自动换票并透传')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    api_key = os.getenv('EVAL_PLATFORM_API_KEY', '')
    cases = load_cases(args.cases)
    run_id = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout, verify=not args.insecure) as client:
        cs_token = customer_credential(client, args.customer_mock) if args.customer_mock else ''
        for case in cases:
            started = time.perf_counter()
            row: dict[str, Any] = {'id': case['id'], 'message': case['message']}
            try:
                if cs_token and case.get('customer_credential'):
                    # 客服场景必须走契约 §6 的身份链：cs1 → customer-token，不得与平台 token 混用。
                    identity = customer_identity(client, base, cs_token)
                    chat_headers = {'Authorization': 'Bearer ' + identity['token'],
                                    'X-Customer-Credential': cs_token}
                else:
                    identity = exchange_token(client, base, f'eval_{run_id}_{case["id"]}_{uuid.uuid4().hex[:6]}', api_key)
                    chat_headers = {'Authorization': 'Bearer ' + identity['token']}
                response = client.post(
                    base + '/chat', headers=chat_headers,
                    json={'user_id': identity['user_id'], 'message': case['message']},
                )
                response.raise_for_status()
                payload = response.json()
                row.update(evaluate_response(case, payload))
                row.update({
                    'ui': payload.get('ui'),
                    'tools': tool_names(payload), 'reply': str(payload.get('reply') or '')[:500],
                    'stage': payload.get('stage'), 'session_id': payload.get('session_id'),
                })
            except Exception as exc:  # 单条失败不终止整批
                row.update({'passed': False, 'machine_passed': False, 'status': 'failed',
                            'manual_checks': 0, 'manual_check_items': [],
                            'issues': [f'{type(exc).__name__}: {str(exc)[:300]}']})
            row['elapsed_seconds'] = round(time.perf_counter() - started, 3)
            results.append(row)
            print(row['status'].upper(), row['id'], row['elapsed_seconds'], row.get('issues') or '')
    report = {
        'run_id': run_id, 'base_url': base, 'case_file': str(args.cases),
        'summary': {
            'total': len(results),
            'passed': sum(row['status'] == 'passed' for row in results),
            'needs_review': sum(row['status'] == 'needs_review' for row in results),
            'failed': sum(row['status'] == 'failed' for row in results),
        },
        'results': results,
    }
    output = args.output or (RESULTS / f'{run_id}.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('report:', output)
    return 1 if args.strict and report['summary']['passed'] != report['summary']['total'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
