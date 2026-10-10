"""The safety formatter remains independently importable and wired to the pipeline."""
from agent.safety import formatter


def test_independent_formatter_strips_internal_leaks_before_escaping():
    dirty = 'platform_db_query_entity 的结果：\n{"password":"hidden"}\n<script>alert(1)</script>'
    cleaned = formatter._strip_internal_leak(dirty)
    rendered = formatter._neutralize_dangerous_tags(cleaned)
    assert 'platform_db_query_entity' not in rendered
    assert 'password' not in rendered
    assert '&lt;script&gt;' in rendered
    assert '<script>' not in rendered


def test_compatibility_aliases_reference_independent_formatter():
    from agent import agent as module
    assert module._strip_separator_lines is formatter._strip_separator_lines
    assert module._strip_internal_leak is formatter._strip_internal_leak
    assert module._neutralize_dangerous_tags is formatter._neutralize_dangerous_tags
    assert [step.fn for step in module._CLEANUP_PIPELINE[:3]] == [
        formatter._strip_separator_lines,
        formatter._strip_internal_leak,
        formatter._neutralize_dangerous_tags,
    ]
