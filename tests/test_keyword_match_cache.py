"""关键词多词匹配 / 零命中放宽 / HTTP TTL 缓存（2026-09-18，A-4 前提修复）。

背景（线上实测，演示环境）：
    用户「送妈妈一束花，预算200」→ 模型连查 3 次平台，共 35.4s：
      1. keyword="妈妈 康乃馨"  → data: []   ← 白烧一轮（6.5s）
      2. keyword="母亲节 花束"   → data: []   ← 白烧一轮（6.5s）
      3. 不带 keyword           → 拿到数据

根因两条：
    · 旧 `_match` 把 keyword **整串**做子串匹配 —— 「妈妈 康乃馨」这串字面在商品名/描述里
      不可能出现，必然返空；模型于是换词再试，白烧一次 LLM 往返。
    · 平台列表接口不支持查询参数（过滤在本地做），每次 fetch 都是「拉全量」，
      一轮咨询查 3 次 = 3 次网络请求。

本测试锁定修复后的语义，防止回退。
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data_gateway import http_source as hs


@pytest.fixture(autouse=True)
def _clear_cache():
    """缓存是进程级的，测试之间必须隔离。"""
    hs.clear_cache()
    yield
    hs.clear_cache()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('PLATFORM_API_AISTORE_URL', 'https://example.com')
    return 'aistore'


def _payload(rows):
    return {'code': 0, 'data': {'list': rows}}


ROWS = [
    {'id': 'p1', 'name': '感恩母亲·康乃馨花束', 'price': 15800, 'ownerShopId': 's001',
     'description': '送给妈妈的温暖祝福'},
    {'id': 'p2', 'name': '星星花束', 'price': 5800, 'ownerShopId': 's002'},
    {'id': 'p3', 'name': '粉玫瑰花束', 'price': 8800, 'ownerShopId': 's001'},
]


# ── A. 分词（纯函数）──

@pytest.mark.parametrize('raw, expect', [
    ('妈妈 康乃馨', ['妈妈', '康乃馨']),
    ('玫瑰,百合、洋桔梗', ['玫瑰', '百合', '洋桔梗']),
    ('玫瑰/百合+满天星', ['玫瑰', '百合', '满天星']),
    ('  康乃馨  ', ['康乃馨']),
    ('', []),
    ('   ', []),
    (None, []),
])
def test_split_keywords(raw, expect):
    assert hs._split_keywords(raw) == expect


def test_split_keywords_dedupes_keeps_order():
    """去重但保序 —— 顺序影响可读性，去重避免同词重复计分。"""
    assert hs._split_keywords('玫瑰 玫瑰 百合 玫瑰') == ['玫瑰', '百合']


def test_split_keywords_lowercases():
    """英文/大小写统一小写，与 haystack 同口径。"""
    assert hs._split_keywords('Rose ROSE') == ['rose']


# ── B. 多词匹配（核心修复）──

def test_keyword_hits_counts_each_token():
    row = hs.normalize_row('plan', ROWS[0])
    assert hs._keyword_hits(row, ['妈妈', '康乃馨']) == 2
    assert hs._keyword_hits(row, ['康乃馨']) == 1
    assert hs._keyword_hits(row, ['玫瑰']) == 0
    assert hs._keyword_hits(row, []) == 0


def test_multitoken_keyword_no_longer_returns_empty(configured, monkeypatch):
    """回归本次事故：『妈妈 康乃馨』旧实现整串匹配 → 必然空。"""
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(ROWS))
    out = hs.fetch_entity('aistore', 'plan', keyword='妈妈 康乃馨')
    assert [r['id'] for r in out] == ['p1'], '多词应 OR 匹配，命中含「妈妈」或「康乃馨」的行'


def test_multitoken_ranks_more_hits_first(configured, monkeypatch):
    """命中词元多的排前面（更相关）。"""
    rows = [
        {'id': 'few', 'name': '康乃馨礼盒', 'price': 100, 'ownerShopId': 's001'},
        {'id': 'many', 'name': '康乃馨花束', 'price': 100, 'ownerShopId': 's001',
         'description': '送给妈妈'},
    ]
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(rows))
    out = hs.fetch_entity('aistore', 'plan', keyword='妈妈 康乃馨')
    assert [r['id'] for r in out] == ['many', 'few']


def test_single_token_behavior_unchanged(configured, monkeypatch):
    """单 token 走老路径，零退化（既有测试也在守这条）。"""
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(ROWS))
    out = hs.fetch_entity('aistore', 'plan', keyword='粉')
    assert [r['id'] for r in out] == ['p3']


# ── C. 匹配模式标注 ──

def test_match_mode_exact(configured, monkeypatch):
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(ROWS))
    meta: dict = {}
    hs.fetch_entity('aistore', 'plan', keyword='妈妈 康乃馨', meta=meta)
    assert meta['match'] == 'exact'
    assert meta['keyword_tokens'] == ['妈妈', '康乃馨']


def test_match_mode_partial(configured, monkeypatch):
    """只命中部分词元 → partial。"""
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(ROWS))
    meta: dict = {}
    hs.fetch_entity('aistore', 'plan', keyword='康乃馨 向日葵 郁金香', meta=meta)
    assert meta['match'] == 'partial'


def test_match_mode_all_without_keyword(configured, monkeypatch):
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(ROWS))
    meta: dict = {}
    hs.fetch_entity('aistore', 'plan', meta=meta)
    assert meta['match'] == 'all'


# ── D. 零命中放宽（不再返空，避免模型白试一轮）──

def test_zero_hit_relaxes_to_full_set(configured, monkeypatch):
    """关键词一个都没命中 → 返回全部在售 + 标注 relaxed，而不是空。"""
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(ROWS))
    meta: dict = {}
    out = hs.fetch_entity('aistore', 'plan', keyword='蓝色妖姬', meta=meta)
    assert len(out) == 3, '零命中必须放宽（返空会让模型换词再试一轮，白烧 6.5s）'
    assert meta['match'] == 'relaxed'
    assert meta['keyword_tokens'] == ['蓝色妖姬']


def test_relaxed_still_respects_shop_scope(configured, monkeypatch):
    """放宽只放宽关键词 —— shop_id 是硬约束，绝不放宽。"""
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(ROWS))
    out = hs.fetch_entity('aistore', 'plan', keyword='蓝色妖姬', shop_id='s001')
    assert [r['id'] for r in out] == ['p1', 'p3']


def test_relaxed_still_respects_row_id(configured, monkeypatch):
    """主键是硬约束：零命中 + 主键不符 → 空（不能因放宽而漏出别的行）。"""
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(ROWS))
    out = hs.fetch_entity('aistore', 'plan', keyword='蓝色妖姬', row_id='p2')
    assert [r['id'] for r in out] == ['p2']
    out = hs.fetch_entity('aistore', 'plan', keyword='蓝色妖姬', row_id='nope')
    assert out == []


def test_relaxed_respects_limit(configured, monkeypatch):
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(ROWS))
    out = hs.fetch_entity('aistore', 'plan', keyword='蓝色妖姬', limit=2)
    assert len(out) == 2


# ── E. HTTP TTL 缓存 ──

class _Resp:
    status_code = 200
    content = b'{"code": 0, "data": {"list": [{"id": "p1", "name": "x"}]}}'


def _count_requests(monkeypatch, status=200, content=None):
    calls = {'n': 0}

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        calls['n'] += 1
        resp = _Resp()
        resp.status_code = status
        if content is not None:
            resp.content = content
        return resp

    monkeypatch.setattr(httpx, 'get', fake_get)
    return calls


def test_cache_hits_within_ttl(configured, monkeypatch):
    calls = _count_requests(monkeypatch)
    hs._fetch_json('https://example.com/v1/merchant/products')
    hs._fetch_json('https://example.com/v1/merchant/products')
    hs._fetch_json('https://example.com/v1/merchant/products')
    assert calls['n'] == 1, '同一 URL 在 TTL 内只应请求一次'


def test_cache_disabled_when_ttl_zero(configured, monkeypatch):
    monkeypatch.setenv('PLATFORM_HTTP_CACHE_TTL', '0')
    calls = _count_requests(monkeypatch)
    hs._fetch_json('https://example.com/v1/merchant/products')
    hs._fetch_json('https://example.com/v1/merchant/products')
    assert calls['n'] == 2


def test_cache_key_includes_token(configured, monkeypatch):
    """不同 token 必须分开缓存 —— 否则会串数据源。"""
    calls = _count_requests(monkeypatch)
    hs._fetch_json('https://example.com/v1/merchant/products', token='tok-a')
    hs._fetch_json('https://example.com/v1/merchant/products', token='tok-b')
    hs._fetch_json('https://example.com/v1/merchant/products', token='tok-a')
    assert calls['n'] == 2


def test_cache_key_includes_url(configured, monkeypatch):
    calls = _count_requests(monkeypatch)
    hs._fetch_json('https://example.com/v1/merchant/products')
    hs._fetch_json('https://example.com/v1/merchant/shops')
    assert calls['n'] == 2


def test_cache_expires(configured, monkeypatch):
    monkeypatch.setenv('PLATFORM_HTTP_CACHE_TTL', '60')
    calls = _count_requests(monkeypatch)
    hs._fetch_json('https://example.com/v1/merchant/products')
    # 把已写入的缓存时间戳往前拨，模拟过期
    with hs._CACHE_LOCK:
        for key, (ts, payload) in list(hs._CACHE.items()):
            hs._CACHE[key] = (ts - 3600, payload)
    hs._fetch_json('https://example.com/v1/merchant/products')
    assert calls['n'] == 2


def test_clear_cache_forces_refetch(configured, monkeypatch):
    calls = _count_requests(monkeypatch)
    hs._fetch_json('https://example.com/v1/merchant/products')
    hs.clear_cache()
    hs._fetch_json('https://example.com/v1/merchant/products')
    assert calls['n'] == 2


def test_cache_survives_repeated_fetch_entity(configured, monkeypatch):
    """端到端：模型查 3 次（不同关键词）只应产生 1 次真实网络请求。"""
    import json as _json

    calls = _count_requests(monkeypatch, content=_json.dumps(_payload(ROWS)).encode())
    hs.fetch_entity('aistore', 'plan', keyword='妈妈 康乃馨')
    hs.fetch_entity('aistore', 'plan', keyword='母亲节 花束')
    hs.fetch_entity('aistore', 'plan')
    assert calls['n'] == 1, '三次查询共用一次网络请求（过滤在本地做，平台数据没变）'


def test_cache_ttl_invalid_env_falls_back(configured, monkeypatch):
    monkeypatch.setenv('PLATFORM_HTTP_CACHE_TTL', 'not-a-number')
    assert hs.cache_ttl() == 60.0
