"""Focused offline validation checks for the committed evaluation format."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_eval_set import DEFAULT_CASES, validate


@pytest.fixture
def case():
    return json.loads(DEFAULT_CASES.read_text(encoding='utf-8').splitlines()[0])


def write_cases(tmp_path: Path, *cases: dict) -> Path:
    path = tmp_path / 'cases.jsonl'
    path.write_text(''.join(json.dumps(case, ensure_ascii=False) + '\n' for case in cases), encoding='utf-8')
    return path


def test_committed_suite_including_customer_service_cases():
    assert validate() == 12


@pytest.mark.parametrize(('field', 'value', 'error'), [
    ('intent', 'not_an_intent', 'invalid intent'),
    ('expected_ui', 'not_a_ui', 'invalid expected_ui'),
    ('allowed_tools', ['made_up_tool'], 'invalid allowed_tools'),
    ('forbidden_tools', ['made_up_tool'], 'invalid forbidden_tools'),
    ('allowed_tools', 'retrieve_knowledge', 'invalid allowed_tools'),
    ('forbidden_tools', [None], 'invalid forbidden_tools'),
    ('checks', [], 'invalid checks'),
    ('checks', ['  '], 'invalid checks'),
    ('checks', 'not a list', 'invalid checks'),
    ('max_iterations', True, 'invalid max_iterations'),
])
def test_invalid_fields_fail(tmp_path, case, field, value, error):
    case[field] = value
    with pytest.raises(ValueError, match=f'line 1: {error}'):
        validate(write_cases(tmp_path, case))


def test_empty_suite_fails(tmp_path):
    path = tmp_path / 'empty.jsonl'
    path.write_text('\n \n', encoding='utf-8')
    with pytest.raises(ValueError, match='empty evaluation suite'):
        validate(path)
    result = subprocess.run(
        [sys.executable, str(DEFAULT_CASES.parent.parent / 'scripts' / 'validate_eval_set.py'),
         '--cases', str(path)], capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert 'empty evaluation suite' in result.stderr


def test_duplicate_id_and_overlapping_tools_fail(tmp_path, case):
    with pytest.raises(ValueError, match='line 2: duplicate id'):
        validate(write_cases(tmp_path, case, copy.deepcopy(case)))
    case['forbidden_tools'].append(case['allowed_tools'][0])
    with pytest.raises(ValueError, match='line 1: tool in both allow/forbid'):
        validate(write_cases(tmp_path, case))


def test_non_object_and_malformed_json_fail(tmp_path):
    path = tmp_path / 'bad.jsonl'
    path.write_text('[]\n', encoding='utf-8')
    with pytest.raises(ValueError, match='scenario must be an object'):
        validate(path)
    path.write_text('{bad\n', encoding='utf-8')
    with pytest.raises(ValueError, match='invalid JSON'):
        validate(path)
