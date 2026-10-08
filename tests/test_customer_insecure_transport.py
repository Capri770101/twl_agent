"""CUSTOMER_SERVICE_ALLOW_INSECURE（dev-only HTTP 放行）的契约测试。

为什么重要
----------
老板封禁 H5 生产环境（2026-10-08）后，本地联调只能走 mock 后端（HTTP）。
放行开关必须满足三条不变式：

1. 默认（dev 未显式开）：HTTP 仍拒绝 —— 和生产行为一致；
2. dev + 显式 true：HTTP 放行，HTTPS 不受影响；
3. **生产 + 显式 true：强制失效（fail-closed）** —— 误配也不能把票据送进明文通道。
"""
from __future__ import annotations

import asyncio

import pytest

from backend.config import Settings
from backend.customer_service import query_orders
from backend.customer_shops import query_shops


def _settings(**kw) -> Settings:
    base = dict(APP_ENV='dev', DATABASE_URL='sqlite://', CUSTOMER_SERVICE_ENABLED=True,
                CUSTOMER_SERVICE_SECRET='x' * 32,
                CUSTOMER_SERVICE_BASE_URL='http://127.0.0.1:4761/api/customer-service')
    base.update(kw)
    return Settings(**base)


def test_dev_default_rejects_http(monkeypatch):
    """dev 但未显式 ALLOW_INSECURE → HTTP 基址仍返回 UNSUPPORTED。"""
    s = _settings()
    monkeypatch.setattr('backend.customer_service.settings', s)
    monkeypatch.setattr('backend.customer_shops.settings', s)
    assert asyncio.run(query_orders('cs1.anything')) == {'ok': False, 'code': 'UNSUPPORTED', 'retryable': False}
    assert asyncio.run(query_shops()) == {'ok': False, 'code': 'UNSUPPORTED', 'retryable': False}


def test_dev_explicit_flag_allows_http(monkeypatch):
    """dev + ALLOW_INSECURE=true → HTTP 基址通过传输校验（参数校验继续生效）。"""
    s = _settings(CUSTOMER_SERVICE_ALLOW_INSECURE=True)
    monkeypatch.setattr('backend.customer_service.settings', s)
    monkeypatch.setattr('backend.customer_shops.settings', s)
    # 无票据 → AUTH_REQUIRED（说明已越过传输校验，进入了凭据校验）
    assert asyncio.run(query_orders('')) == {'ok': False, 'code': 'AUTH_REQUIRED', 'retryable': False}


def test_prod_flag_is_fail_closed():
    """生产环境：validator 强制把 ALLOW_INSECURE 归 false，误配无效。"""
    s = _settings(APP_ENV='prod', DATABASE_URL='postgresql://u@h/db', JWT_SECRET='y' * 32,
                  AUTH_REQUIRED=True, ALLOWED_ORIGINS='https://h5.tiaowulan.com',
                  CUSTOMER_SERVICE_ALLOW_INSECURE=True)
    assert s.CUSTOMER_SERVICE_ALLOW_INSECURE is False


def test_prod_https_unchanged():
    """生产 + HTTPS 基址：开关归 false 不影响正常 HTTPS 校验路径。"""
    s = _settings(APP_ENV='prod', DATABASE_URL='postgresql://u@h/db', JWT_SECRET='y' * 32,
                  AUTH_REQUIRED=True, ALLOWED_ORIGINS='https://h5.tiaowulan.com',
                  CUSTOMER_SERVICE_BASE_URL='https://h5.example.com/api/customer-service')
    assert s.CUSTOMER_SERVICE_ALLOW_INSECURE is False
