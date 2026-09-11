"""入口上下文规范化（normalize_*）的边界测试。

为什么重要
----------
这几个函数是「锁店 / 全平台」判定的第一道闸门，历史 bug 就出在这里：
- I-1：前端从首页传入 `shop_id='default'` 被当成真实店铺 → 会话被锁进不存在的店铺 → 查什么都查不到。
因此占位值必须被系统性覆盖，且真实值不能被误伤。
"""
from __future__ import annotations

import pytest

from agent.ports import normalize_entry, normalize_product_id, normalize_shop_id

PLACEHOLDER_SHOP_IDS = [
    None, '', '   ', 'default', 'DEFAULT', 'Default',
    'none', 'NONE', 'null', 'undefined', '0', '-',
]


@pytest.mark.parametrize('raw', PLACEHOLDER_SHOP_IDS)
def test_placeholder_shop_id_becomes_none(raw) -> None:
    assert normalize_shop_id(raw) is None, f'{raw!r} 应被规范为未锁店'


@pytest.mark.parametrize('raw,expected', [
    ('S001', 'S001'),
    (' shop12 ', 'shop12'),
    ('Shop-9', 'Shop-9'),
    ('store_001', 'store_001'),
])
def test_real_shop_id_preserved(raw, expected) -> None:
    assert normalize_shop_id(raw) == expected


@pytest.mark.parametrize('raw', [None, '', '   ', 'default', '0', 'undefined'])
def test_placeholder_product_id_becomes_none(raw) -> None:
    assert normalize_product_id(raw) is None


def test_real_product_id_preserved() -> None:
    assert normalize_product_id('P88') == 'P88'


@pytest.mark.parametrize('entry,shop_id,product_id,expected', [
    (None, None, None, 'home'),
    (None, 'S001', None, 'shop'),
    ('shop', 'S001', None, 'shop'),
    (None, 'S001', 'P88', 'product'),
    ('product', 'S001', 'P88', 'product'),
    ('home', 'S001', None, 'home'),          # 显式 home 优先
    ('product', None, None, 'home'),         # 声称商品页但无商品无店铺 → 不锁
    ('shop', None, None, 'home'),            # 声称店铺页但无店铺 → 不锁
    ('default', None, None, 'home'),
])
def test_normalize_entry(entry, shop_id, product_id, expected) -> None:
    assert normalize_entry(entry, shop_id, product_id) == expected
