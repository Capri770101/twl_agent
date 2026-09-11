"""回复清理（排版去噪 + 隐私兜底）的回归测试。

为什么重要
----------
- I-2：模型曾用 `---` 分隔线分段，前端不渲染 markdown，用户看到一排破折号。
- 隐私约束：prompt 是软约束，必须有 deterministic 兜底，确保数据库查询结果与内部实现
  （工具名 / 内部字段 / 原始 JSON）不会漏到前端。
"""
from __future__ import annotations

import pytest

from agent.agent import _strip_internal_leak, _strip_separator_lines

SEPARATORS = ['---', '***', '——', '___', '===', '  ---  ']


@pytest.mark.parametrize('sep', SEPARATORS)
def test_separator_line_removed(sep: str) -> None:
    text = f'一、标题\n{sep}\n1. 内容'
    out = _strip_separator_lines(text)
    assert '一、标题' in out
    assert '1. 内容' in out
    assert sep.strip() not in out


def test_inline_dashes_not_removed() -> None:
    """行内的价格区间（如 300----500 元）不是独立分隔线，不能误删。"""
    text = '价格是 300----500 元'
    assert _strip_separator_lines(text) == text


def test_normal_text_untouched_by_separator_strip() -> None:
    text = '推荐「千百度花坊」，评分 4.8，营业中～'
    assert _strip_separator_lines(text) == text


def test_empty_and_non_string_passthrough() -> None:
    assert _strip_separator_lines('') == ''
    assert _strip_separator_lines(None) is None


def test_tool_name_replaced_with_neutral_term() -> None:
    out = _strip_internal_leak('我通过 platform_db_query_entity 查到了 3 家店')
    assert 'platform_db_query_entity' not in out
    assert '平台查询' in out


def test_source_id_replaced() -> None:
    out = _strip_internal_leak('source_id=flower 的结果如下')
    assert 'source_id' not in out


def test_platform_env_var_replaced() -> None:
    out = _strip_internal_leak('读取 PLATFORM_DB_S001_URL 失败')
    assert 'PLATFORM_DB_S001_URL' not in out


def test_raw_json_line_dropped() -> None:
    text = '查询结果：\n{"ok": true, "rows": [{"name": "花店A", "phone": "123"}]}\n以上是全部'
    out = _strip_internal_leak(text)
    assert '{"ok"' not in out
    assert '查询结果' in out
    assert '以上是全部' in out


def test_normal_reply_untouched_by_leak_strip() -> None:
    text = '推荐「千百度花坊」，评分 4.8，营业中，配送费 0 元～'
    assert _strip_internal_leak(text) == text


def test_empty_and_non_string_passthrough_for_leak_strip() -> None:
    assert _strip_internal_leak('') == ''
    assert _strip_internal_leak(None) is None
