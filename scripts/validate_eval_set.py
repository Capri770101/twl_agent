"""离线校验智能体场景评测集，避免评测样本自身失真。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
path = ROOT / 'evals' / 'flower_scenarios.jsonl'
required = {'id', 'message', 'intent', 'expected_ui', 'allowed_tools', 'forbidden_tools', 'max_iterations', 'checks'}
seen: set[str] = set()
rows = []
for line_no, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
    if not raw.strip():
        continue
    item = json.loads(raw)
    missing = required - set(item)
    if missing:
        raise SystemExit(f'line {line_no}: missing {sorted(missing)}')
    if item['id'] in seen:
        raise SystemExit(f'line {line_no}: duplicate id {item["id"]}')
    if item['intent'] not in {'qa', 'buying', 'design', 'image', 'greeting'}:
        raise SystemExit(f'line {line_no}: invalid intent')
    if not isinstance(item['max_iterations'], int) or not 1 <= item['max_iterations'] <= 8:
        raise SystemExit(f'line {line_no}: invalid max_iterations')
    overlap = set(item['allowed_tools']) & set(item['forbidden_tools'])
    if overlap:
        raise SystemExit(f'line {line_no}: tool in both allow/forbid: {sorted(overlap)}')
    seen.add(item['id'])
    rows.append(item)
print(f'valid evaluation scenarios: {len(rows)}')
