"""「店铺可提供原料」的提取与标注测试。

背景（2026-09-17，Capri 需求）：锁店场景下 DIY 方案用的原料要是该店能买到的，
否则客户拿着方案配不齐。平台没有结构化花材清单，只能从商品文案提取。
Capri 决定：缺料**保留花材、只标注**，不自动替换。
"""

from __future__ import annotations

import pytest

from agent import shop_materials
from agent.shop_materials import available_materials, clear_cache, materials_in_text
from agent.tools import _shop_scope_rule, annotate_shop_materials


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


# ── 从文案提取花材 ────────────────────────────────────────────────────────

def test_extracts_canonical_name_from_alias() -> None:
    """「粉玫瑰」要归到规范名「玫瑰」，否则方案里同名花会被当成两种。"""
    hit = materials_in_text('11枝粉玫瑰花混搭')
    assert '玫瑰' in hit
    assert '粉玫瑰' not in hit


def test_extracts_multiple_materials() -> None:
    hit = materials_in_text('33朵粉康乃馨配满天星与尤加利叶')
    assert {'康乃馨', '满天星', '尤加利'} <= hit


def test_ignores_non_flower_text() -> None:
    assert materials_in_text('随机花瓶一个') == set()
    assert materials_in_text('') == set()


# ── 店铺花材集合：必须区分「空」与「未知」 ──────────────────────────────

def test_none_when_fetch_fails(monkeypatch) -> None:
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: None)
    assert available_materials('s001') is None


def test_none_for_blank_shop_id() -> None:
    assert available_materials('') is None
    assert available_materials('   ') is None


def test_empty_set_is_meaningful(monkeypatch) -> None:
    """查到了但提取不出花材 → 空集，与「查询失败」的 None 语义不同。"""
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: set())
    assert available_materials('s001') == set()


def test_result_is_cached(monkeypatch) -> None:
    calls: list[str] = []

    def fake(sid: str) -> set[str]:
        calls.append(sid)
        return {'玫瑰'}

    monkeypatch.setattr(shop_materials, '_fetch', fake)
    assert available_materials('s9') == {'玫瑰'}
    assert available_materials('s9') == {'玫瑰'}
    assert calls == ['s9']


# ── 方案标注 ──────────────────────────────────────────────────────────────

def _plan() -> dict:
    return {
        'shop_id': 's1',
        'design': {
            'main_flowers': [{'name': '康乃馨', 'qty': 11}],
            'fillers': [{'name': '勿忘我', 'qty': 3}],
            'foliage': [{'name': '尤加利', 'qty': 2}],
        },
    }


def test_marks_missing_materials(monkeypatch) -> None:
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: {'康乃馨', '尤加利'})
    out = annotate_shop_materials(_plan(), 's1')
    assert out['design']['main_flowers'][0]['in_shop'] is True
    assert out['design']['fillers'][0]['in_shop'] is False
    assert out['design']['foliage'][0]['in_shop'] is True
    assert out['unavailable_materials'] == ['勿忘我']


def test_keeps_flowers_unchanged(monkeypatch) -> None:
    """Capri 明确：缺料只标注，不自动替换 —— 花名与支数都不能被改。"""
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: {'康乃馨'})
    out = annotate_shop_materials(_plan(), 's1')
    assert out['design']['fillers'][0]['name'] == '勿忘我'
    assert out['design']['fillers'][0]['qty'] == 3


def test_unknown_shop_does_not_mark(monkeypatch) -> None:
    """查询失败（None）时一律不标注，否则会把「查不到」说成「该店没有」。"""
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: None)
    out = annotate_shop_materials(_plan(), 's1')
    assert 'in_shop' not in out['design']['main_flowers'][0]
    assert 'unavailable_materials' not in out


def test_no_shop_id_skips_marking(monkeypatch) -> None:
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: {'康乃馨'})
    plan = {'design': {'main_flowers': [{'name': '勿忘我'}]}}
    out = annotate_shop_materials(plan, '')
    assert 'in_shop' not in out['design']['main_flowers'][0]


def test_alias_in_plan_still_matches(monkeypatch) -> None:
    """方案里写「粉色康乃馨」而清单里是「康乃馨」→ 应判为可获得。"""
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: {'康乃馨'})
    plan = {'design': {'main_flowers': [{'name': '粉色康乃馨', 'qty': 11}]}}
    out = annotate_shop_materials(plan, 's1')
    assert out['design']['main_flowers'][0]['in_shop'] is True


def test_no_missing_means_no_field(monkeypatch) -> None:
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: {'康乃馨', '勿忘我', '尤加利'})
    out = annotate_shop_materials(_plan(), 's1')
    assert 'unavailable_materials' not in out


def test_tolerates_broken_design(monkeypatch) -> None:
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: {'康乃馨'})
    assert annotate_shop_materials({'shop_id': 's1'}, 's1') == {'shop_id': 's1'}
    assert annotate_shop_materials({'shop_id': 's1', 'design': None}, 's1')['design'] is None


# ── 设计约束文案 ──────────────────────────────────────────────────────────

def test_scope_rule_lists_real_materials(monkeypatch) -> None:
    """清单必须真的写进约束里 —— 这正是上次失效的地方（让模型自己查，它不查）。"""
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: {'玫瑰', '康乃馨'})
    rule = _shop_scope_rule('s1')
    assert '玫瑰' in rule and '康乃馨' in rule
    assert '优先从这份清单里选' in rule
    # 不再把「去查商品」当作获取花材的手段（商品不等于花材）
    assert '再据此设计' not in rule


def test_scope_rule_unknown_does_not_promise(monkeypatch) -> None:
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: None)
    rule = _shop_scope_rule('s1')
    assert '不要声称' in rule


def test_scope_rule_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(shop_materials, '_fetch', lambda sid: set())
    rule = _shop_scope_rule('s1')
    assert '没有可识别的花材用料信息' in rule
