"""附加 IP 维度限流的单测（演示页防刷，2026-09-16）。

背景：演示页每次访问都会 `POST /auth/anonymous` 领一个**全新 user_id**，
只按 user_id 限流等于没有上限 → 需要一道按来源 IP 的附加限流。
生产（`RATE_LIMIT_IP_PER_MINUTE=0`）行为零变化，这里保护的是「附加维度确实独立生效」。
"""
from __future__ import annotations

import uuid

from backend.rate_limit import check_rate_limit


def test_custom_quota_is_enforced():
    """自定义配额（如按 IP 的 2 次/分）应独立生效。"""
    key = f'test-ip:{uuid.uuid4().hex}'
    assert check_rate_limit(key, per_minute=2)[0] is True
    assert check_rate_limit(key, per_minute=2)[0] is True
    allowed, retry_after = check_rate_limit(key, per_minute=2)
    assert allowed is False
    assert retry_after >= 1


def test_custom_quota_isolated_per_key():
    """不同 IP 各自计数，互不影响。"""
    k1, k2 = f'ip:{uuid.uuid4().hex}', f'ip:{uuid.uuid4().hex}'
    assert check_rate_limit(k1, per_minute=1)[0] is True
    assert check_rate_limit(k2, per_minute=1)[0] is True
    assert check_rate_limit(k1, per_minute=1)[0] is False
    assert check_rate_limit(k2, per_minute=1)[0] is False


def test_custom_quota_does_not_consume_default_quota():
    """附加维度用独立计数器：占用 IP 配额不应影响同 key 的默认配额计数。"""
    key = f'mixed:{uuid.uuid4().hex}'
    for _ in range(5):
        check_rate_limit(key, per_minute=1)   # 消耗附加配额（第 2 次起被拒）
    # 默认配额（settings 默认 30）仍应放行——说明两者互不污染
    assert check_rate_limit(key)[0] is True
