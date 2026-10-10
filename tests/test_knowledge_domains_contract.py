"""Public knowledge tool domains must match nonempty persisted data sources."""
from agent.knowledge.store import query_knowledge, load_manifest, _DOMAINS
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
