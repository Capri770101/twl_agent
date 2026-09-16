"""system prompt 构建的回归测试。

为什么重要
----------
prompt 是智能体行为的核心资产，体量大（~7000 字符）、段落多。历史上多次出现
「改一处、弄丢另一处」的风险（例如删除工具说明书段时，标题里仍残留
「自动附在文末」的过期描述）。这里用结构断言 + 体积护栏兜住。
"""
from __future__ import annotations

import pytest

from agent.agent import ReActAgent

# prompt 中必须存在的关键段 —— 删任何一个都会造成可观察的行为退化
REQUIRED_SECTIONS = [
    '## 核心原则',
    '## 怎么选工具',
    '## 必须守住的事',
    '## 核心工具速览',
    '## respond_to_user 参数说明',
    '## 回复格式',
    '## 隐私与内部信息',
]


@pytest.mark.parametrize('leak', [
    'open_status_text', 'is_open_now', 'ownerShopId', 'subMchId', 'shop_id',
    'delivery_fee', 'min_order_price', 'business_hours', 'ratingCount',
])
def test_prompt_has_no_platform_field_identifiers(leak: str) -> None:
    """prompt 里不应出现平台原始字段名 —— 它们会诱导模型向用户复述数据结构。

    背景（Capri 2026-09-16 要求）：不能让模型把数据库内容（表名/字段名/结构）全盘托出。
    旧版 `base.md` 的场景 6 里逐条列着 `open_status_text` / `is_open_now` /
    `delivery_fee` / `min_order_price` 等字段标识符，等于把结构递到模型嘴边。
    去脚本化的同时把这些标识符一并清掉，只保留**业务含义**（营业状态 / 配送费 / 起送价…）。
    """
    assert leak not in _prompt(), f'prompt 里出现了平台原始字段名：{leak}'


def _prompt(**kwargs) -> str:
    """构建未锁店 / 默认入口的 system prompt。"""
    return ReActAgent._build_system(None, None, {}, **kwargs)


@pytest.mark.parametrize('section', REQUIRED_SECTIONS)
def test_required_section_present(section: str) -> None:
    assert section in _prompt(), f'prompt 缺少关键段：{section}'


def test_tool_manual_section_removed() -> None:
    """工具定义由 function-calling 的 tools 参数提供，prompt 内不再重复注入说明书。"""
    assert '## 工具说明书' not in _prompt()


def test_no_stale_manual_reference() -> None:
    """删掉说明书段后，标题里不应残留「自动附在文末」之类的过期描述。"""
    assert '自动附在文末' not in _prompt()


def test_full_platform_mode_when_no_shop() -> None:
    prompt = _prompt()
    assert '## 全平台模式' in prompt
    assert '## 店铺锁定模式' not in prompt


def test_shop_lock_mode() -> None:
    prompt = _prompt(shop_id='S001', entry='shop')
    assert '## 店铺锁定模式' in prompt
    assert '## 全平台模式' not in prompt
    assert 'S001' in prompt


def test_product_entry_injects_product_context() -> None:
    prompt = _prompt(shop_id='S001', entry='product', product_id='P88', product_title='红玫瑰花束')
    assert '## 用户正在查看的商品' in prompt
    assert 'P88' in prompt
    assert '红玫瑰花束' in prompt


def test_shop_entry_has_no_product_context() -> None:
    assert '## 用户正在查看的商品' not in _prompt(shop_id='S001', entry='shop')


def test_long_term_memory_injected() -> None:
    prompt = ReActAgent._build_system(None, None, {'preferred_color': '粉'}, None)
    assert '用户偏好记忆' in prompt
    assert 'preferred_color=粉' in prompt


def test_privacy_section_states_boundary() -> None:
    """隐私段必须同时说明「不泄露内部信息」与「不要因此少说话」两方面，避免过度抑制。"""
    prompt = _prompt()
    assert '后台静默调用' in prompt or '静默调用' in prompt
    assert '不要只回' in prompt or '不是让你少说话' in prompt


def test_prompt_size_budget() -> None:
    """体积护栏：prompt 明显膨胀时报警（当前约 7.0k，阈值留约 25% 余量）。"""
    assert len(_prompt()) < 9000


# ── prompt 模板（agent/prompts/*.md）──
# 文本已从 _build_system 外置到模板文件；缺任何一个都会让运行期抛 FileNotFoundError。

PROMPT_TEMPLATES = [
    'base',
    'platform_sources',
    'platform_none',
    'stage_image_gen',
    'shop_lock',
    'full_platform',
    'full_platform_plan_only',
    'product_context',
]


@pytest.mark.parametrize('name', PROMPT_TEMPLATES)
def test_prompt_template_file_exists(name: str) -> None:
    import os

    from agent import agent as agent_module

    path = os.path.join(agent_module._PROMPT_DIR, f'{name}.md')
    assert os.path.exists(path), f'缺少 prompt 模板文件：{name}.md'
    assert os.path.getsize(path) > 0, f'prompt 模板为空：{name}.md'


def test_prompt_templates_cover_all_dynamic_sections() -> None:
    """锁店 / 商品页等动态分支渲染后必须真的带上对应内容。"""
    assert '店铺锁定模式' in _prompt(shop_id='S001', entry='shop')
    assert '全平台模式' in _prompt()
    assert '用户正在查看的商品' in _prompt(shop_id='S1', entry='product', product_id='P1', product_title='红花')
    assert 'PLATFORM_DB_<SOURCE_ID>_URL' in _prompt()


def test_plan_only_variant_when_shop_entity_disabled(monkeypatch) -> None:
    """体验/演示实例（平台可查实体不含 shop）应换成「只做方案与建议」的变体。

    否则 prompt 会引导模型去查店铺，而 schema 里已经没有 shop → 白跑一轮还被拒。
    """
    from backend.config import settings

    monkeypatch.setattr(settings, 'PLATFORM_ALLOWED_ENTITIES', 'plan', raising=False)
    prompt = _prompt()
    assert '体验版：只做方案设计与建议' in prompt
    assert '推荐花店' not in prompt  # 原 full_platform 段的「推荐花店」引导不应出现


def test_full_platform_variant_by_default(monkeypatch) -> None:
    """未限制实体（生产默认）时保持原 full_platform 行为。"""
    from backend.config import settings

    monkeypatch.setattr(settings, 'PLATFORM_ALLOWED_ENTITIES', '', raising=False)
    assert '推荐花店' in _prompt()
