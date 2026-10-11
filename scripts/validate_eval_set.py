"""Validate the offline scenario suite without importing the application or its tools."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = ROOT / 'evals' / 'flower_scenarios.jsonl'
REQUIRED = {'id', 'message', 'intent', 'expected_ui', 'allowed_tools',
            'forbidden_tools', 'max_iterations', 'checks'}


def _registered_tools() -> set[str]:
    """Read literal @register_tool names, avoiding agent import side effects."""
    names: set[str] = set()
    for source in (ROOT / 'agent').rglob('*.py'):
        tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Name) or decorator.func.id != 'register_tool':
                    continue
                for keyword in decorator.keywords:
                    if keyword.arg == 'name' and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        names.add(keyword.value.value)
    return names


def _ui_types() -> set[str]:
    source = ROOT / 'agent' / 'engine' / 'ui_protocol.py'
    tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
    return {node.value.value for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == 'UIType'
            for node in cls.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)}


def _intents() -> set[str]:
    source = ROOT / 'agent' / 'orchestration' / 'policies' / 'routing.json'
    def reject_constant(value: str) -> None:
        raise ValueError(f'non-standard routing JSON constant: {value}')

    records = json.loads(source.read_text(encoding='utf-8'), parse_constant=reject_constant)
    if not isinstance(records, list) or not records:
        raise ValueError('routing policy must be a non-empty array')
    names: set[str] = set()
    policy_keys: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {'name', 'route_name', 'max_iterations', 'tools'}:
            raise ValueError('routing policy has invalid fields')
        if any(not isinstance(record[key], str) or not record[key].strip() for key in ('name', 'route_name')):
            raise ValueError('routing policy has invalid name')
        if record['name'] in policy_keys:
            raise ValueError(f'duplicate routing policy: {record["name"]}')
        policy_keys.add(record['name'])
        if type(record['max_iterations']) is not int or record['max_iterations'] <= 0:
            raise ValueError('routing policy has invalid max_iterations')
        tools = record['tools']
        if not isinstance(tools, list) or not tools or any(not isinstance(tool, str) or not tool.strip() for tool in tools) or len(tools) != len(set(tools)):
            raise ValueError('routing policy has invalid tools')
        names.add(record['route_name'])
    return names


def validate(path: Path = DEFAULT_CASES) -> int:
    tools, uis, intents = _registered_tools(), _ui_types(), _intents()
    seen: set[str] = set()
    count = 0
    for line_no, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not raw.strip():
            continue
        prefix = f'line {line_no}'

        def fail(reason: str) -> None:
            raise ValueError(f'{prefix}: {reason}')

        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            fail(f'invalid JSON: {exc.msg}')
        if not isinstance(item, dict):
            fail('scenario must be an object')
        missing = REQUIRED - item.keys()
        if missing:
            fail(f'missing {sorted(missing)}')
        if not isinstance(item['id'], str) or not item['id'].strip():
            fail('invalid id')
        if item['id'] in seen:
            fail(f'duplicate id {item["id"]}')
        if not isinstance(item['message'], str) or not item['message'].strip():
            fail('invalid message')
        if not isinstance(item['intent'], str) or item['intent'] not in intents:
            fail(f'invalid intent {item["intent"]!r}')
        if not isinstance(item['expected_ui'], str) or item['expected_ui'] not in uis:
            fail(f'invalid expected_ui {item["expected_ui"]!r}')
        if type(item['max_iterations']) is not int or not 1 <= item['max_iterations'] <= 8:
            fail('invalid max_iterations')
        for field in ('allowed_tools', 'forbidden_tools'):
            values = item[field]
            if not isinstance(values, list) or any(not isinstance(v, str) or not v or v not in tools for v in values):
                fail(f'invalid {field}: expected registered tool names')
            if len(values) != len(set(values)):
                fail(f'duplicate {field}')
        overlap = set(item['allowed_tools']) & set(item['forbidden_tools'])
        if overlap:
            fail(f'tool in both allow/forbid: {sorted(overlap)}')
        if not isinstance(item['checks'], list) or not item['checks'] or any(
                not isinstance(check, (str, dict)) for check in item['checks']):
            fail('invalid checks: expected non-empty list of strings or assertion objects')
        for check in item['checks']:
            if isinstance(check, str):
                if not check.strip():
                    fail('invalid checks: blank string entry')
                continue
            if set(check) != {'type', 'value'}:
                fail('invalid checks: assertion must have exactly {type, value}')
            if check['type'] not in ('must_include', 'must_not_include', 'ui_is', 'tool_used', 'tool_not_used'):
                fail(f'invalid checks: unknown assertion type {check["type"]!r}')
            if not isinstance(check['value'], str) or not check['value'].strip():
                fail('invalid checks: assertion value must be a non-blank string')
            if check['type'] in ('ui_is',) and check['value'] not in uis:
                fail(f'invalid checks: ui_is value {check["value"]!r} is not a UI type')
            if check['type'] in ('tool_used', 'tool_not_used') and check['value'] not in tools:
                fail(f'invalid checks: {check["type"]} value {check["value"]!r} is not a registered tool')
        seen.add(item['id'])
        count += 1
    if not count:
        raise ValueError('empty evaluation suite')
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=DEFAULT_CASES)
    args = parser.parse_args()
    try:
        count = validate(args.cases)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'{exc}\n')
    print(f'valid evaluation scenarios: {count}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
