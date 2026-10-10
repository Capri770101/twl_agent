"""Public knowledge tool domains must match nonempty persisted data sources."""
import json
from pathlib import Path

from agent.knowledge.store import query_knowledge, _DOMAINS
from agent.toolkit import TOOL_REGISTRY


def test_colors_domain_is_searchable():
    result = query_knowledge('colors', '香槟')
    assert result['count'] > 0
    assert any(row['name'] == '香槟' and row['hex'].startswith('#') for row in result['results'])


def test_empty_shop_knowledge_domain_is_not_advertised():
    assert 'shop' not in _DOMAINS
    assert query_knowledge('shop', '婚礼')['results'] == []
    spec = TOOL_REGISTRY['retrieve_knowledge']
    text = str(spec.parameters) + spec.description
    assert 'shop(商家智库)' not in text
    assert '找店铺用 shop 域' not in text
    assert 'colors' in text


def test_knowledge_static_domains_are_nonempty_and_consistent():
    """每个静态域 JSON 必须存在、非空，且与清单 core_domains 一致。

    原版本断言 docs/知识库概览.md 中的条目计数（该文档已于 2026-10-11 归档）。
    数据源真相改为直接校验 JSON 本体与 knowledge_manifest.json 的一致性。
    """
    root = Path(__file__).resolve().parents[1]
    knowledge = root / 'agent' / 'knowledge'
    manifest = json.loads((knowledge / 'knowledge_manifest.json').read_text(encoding='utf-8'))
    static_domains = {d for d in _DOMAINS if d != 'proven'}
    assert set(manifest['core_domains']) == static_domains
    total = 0
    for domain in sorted(static_domains):
        filename = _DOMAINS[domain]
        entries = json.loads((knowledge / filename).read_text(encoding='utf-8'))
        assert len(entries) > 0, f'domain {domain} ({filename}) must not be empty'
        total += len(entries)
    assert total > 0
    assert 'shop' not in static_domains
