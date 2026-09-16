"""数据库结构防泄露回归门（Capri 2026-09-16 要求：「不能把数据库的内容如数据表等信息全盘托出」）。

prompt 层已在 `base.md` 加了硬规则（并清掉了原先逐条列出的平台原始字段名），
但 prompt 是**软约束**——这里锁住确定性兜底 `_strip_internal_leak` 对「库内标识」的处理。

纯函数，不连 LLM / DB / 网络。
"""
from __future__ import annotations

from agent.agent import _strip_internal_leak


def test_table_names_neutralized():
    out = _strip_internal_leak('我从 products 表和 shops 表里查到了这些数据。')
    assert 'products' not in out and 'shops' not in out
    assert '平台数据' in out


def test_field_identifiers_neutralized():
    out = _strip_internal_leak('返回的字段有 ownerShopId、delivery_fee、open_status_text 这些。')
    for leak in ('ownerShopId', 'delivery_fee', 'open_status_text'):
        assert leak not in out, leak
    assert '相关字段' in out


def test_sql_statement_removed():
    out = _strip_internal_leak('查询语句是 SELECT id, name FROM products WHERE price < 200。')
    assert 'SELECT' not in out.upper()
    assert 'products' not in out


def test_connection_string_masked():
    out = _strip_internal_leak('连的是 postgresql://flora:pwd@postgres:5432/flora_agent 这个库。')
    assert 'flora:pwd' not in out
    assert '(内部地址)' in out


def test_env_var_masked():
    out = _strip_internal_leak('配置项 PLATFORM_DB_AISTORE_URL 指向平台。')
    assert 'PLATFORM_DB' not in out
    assert 'AISTORE' not in out


def test_normal_business_reply_untouched():
    """普通回复不能被误伤——业务表述（营业时间 / 配送费 / 起送价 / 价格）照常保留。"""
    for text in (
        '营业时间是 07:00-22:00，配送费 5 元，起送价 20 元，评分 4.5。',
        '给你挑了 3 款粉色系花束，价格在 68-158 元之间。',
        '这束用粉玫瑰 11 朵配尤加利叶，适合送妈妈。',
    ):
        assert _strip_internal_leak(text) == text


def test_raw_json_payload_line_removed():
    out = _strip_internal_leak('查到的原始数据：\n{"id": "f_s004_1", "name": "蜜语春晖"}\n以上。')
    assert 'f_s004_1' not in out
    assert '以上。' in out


def test_empty_and_non_string_passthrough():
    assert _strip_internal_leak('') == ''
    assert _strip_internal_leak(None) is None
