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
        elif isinstance(item, dict) and item.get('name'):
            names.append(str(item['name']))
    return names


def judge(case: dict[str, Any], response: dict[str, Any]) -> tuple[bool, list[str]]:
    issues: list[str] = []
    reply = str(response.get('reply') or '').strip()
    if not reply:
        issues.append('empty reply')
    if str(case.get('intent')) == 'qa' and any(phrase in reply for phrase in ('请查看下方卡片', '服务暂时开小差', '思考得太久')):
        issues.append('qa did not provide a substantive answer')
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
    return (not issues, issues)


def exchange_token(client: httpx.Client, base: str, external_id: str, api_key: str) -> dict[str, str]:
    headers = {'X-API-Key': api_key} if api_key else {}
    response = client.post(base + '/auth/token', headers=headers, json={'external_user_id': external_id})
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
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    api_key = os.getenv('EVAL_PLATFORM_API_KEY', '')
    cases = load_cases(args.cases)
    run_id = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout, verify=not args.insecure) as client:
        for case in cases:
            started = time.perf_counter()
            row: dict[str, Any] = {'id': case['id'], 'message': case['message']}
            try:
                identity = exchange_token(client, base, f'eval_{run_id}_{case["id"]}_{uuid.uuid4().hex[:6]}', api_key)
                response = client.post(
                    base + '/chat', headers={'Authorization': 'Bearer ' + identity['token']},
                    json={'user_id': identity['user_id'], 'message': case['message']},
                )
                response.raise_for_status()
                payload = response.json()
                passed, issues = judge(case, payload)
                row.update({
                    'passed': passed, 'issues': issues, 'ui': payload.get('ui'),
                    'tools': tool_names(payload), 'reply': str(payload.get('reply') or '')[:500],
                    'stage': payload.get('stage'), 'session_id': payload.get('session_id'),
                })
            except Exception as exc:  # 单条失败不终止整批
                row.update({'passed': False, 'issues': [f'{type(exc).__name__}: {str(exc)[:300]}']})
            row['elapsed_seconds'] = round(time.perf_counter() - started, 3)
            results.append(row)
            print(('PASS' if row['passed'] else 'FAIL'), row['id'], row['elapsed_seconds'], row.get('issues') or '')
    report = {
        'run_id': run_id, 'base_url': base, 'case_file': str(args.cases),
        'summary': {'total': len(results), 'passed': sum(1 for row in results if row['passed'])},
        'results': results,
    }
    output = args.output or (RESULTS / f'{run_id}.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('report:', output)
    return 1 if args.strict and report['summary']['passed'] != report['summary']['total'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
