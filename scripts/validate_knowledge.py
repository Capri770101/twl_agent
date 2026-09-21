"""知识库质量门：检查 JSON 域、manifest、ID、空内容和基本结构。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / 'agent' / 'knowledge'
manifest = json.loads((KB / 'knowledge_manifest.json').read_text(encoding='utf-8'))
domains = {str(x) for x in manifest.get('core_domains', [])}
errors: list[str] = []
stats: dict[str, int] = {}
for domain in sorted(domains):
    filenames = {'flower': 'flowers.json', 'style': 'styles.json', 'pairing': 'pairings.json', 'scene': 'scenes.json'}
    path = KB / filenames.get(domain, f'{domain}.json')
    if domain == 'scene':
        path = KB / 'scenes.json'
    if not path.exists():
        errors.append(f'{domain}: missing {path.name}')
        continue
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        errors.append(f'{domain}: invalid JSON: {exc}')
        continue
    if not isinstance(data, list) or not data:
        errors.append(f'{domain}: expected non-empty list')
        continue
    ids: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f'{domain}[{index}]: expected object')
            continue
        item_id = str(item.get('id') or '').strip()
        name = str(item.get('name') or item.get('title') or '').strip()
        if domain in {'flower', 'style', 'scene', 'budget', 'packaging', 'care'} and not name:
            errors.append(f'{domain}[{index}]: missing name/title')
        if domain == 'pairing' and not str(item.get('condition') or item.get('recommendation') or '').strip():
            errors.append(f'{domain}[{index}]: missing condition/recommendation')
        if item_id:
            if item_id in ids:
                errors.append(f'{domain}[{index}]: duplicate id {item_id}')
            ids.add(item_id)
    stats[domain] = len(data)
if errors:
    print('\n'.join(errors))
    raise SystemExit(1)
print('knowledge domains valid:', ', '.join(f'{key}={value}' for key, value in stats.items()))
