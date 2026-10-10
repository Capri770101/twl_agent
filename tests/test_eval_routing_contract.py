"""Declared scenarios must not drift from the fast route and tool-domain policy."""
import json

import pytest

from agent.engine.intent import classify
from backend.config import settings
from scripts.validate_eval_set import DEFAULT_CASES


@pytest.mark.parametrize('scenario', [json.loads(line) for line in DEFAULT_CASES.read_text(encoding='utf-8').splitlines() if line.strip()], ids=lambda c: c['id'])
def test_eval_case_fast_route_matches_declared_contract(monkeypatch, scenario):
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    route = classify(scenario['message'])
    assert route is not None, f"{scenario['id']}: no fast route (requires explicit slow-path contract)"
    assert route.name == scenario['intent'], scenario['id']
    assert route.max_iterations == scenario['max_iterations'], scenario['id']
    assert set(scenario['allowed_tools']) <= route.tools, scenario['id']
    assert not set(scenario['forbidden_tools']) & route.tools, scenario['id']
