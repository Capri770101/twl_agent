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
    '## 工具调用指南',
    '### 场景1',
    '### 场景2',
    '### 场景3',
    '### 场景4',
    '### 场景5',
    '### 场景6',
    '### 场景7',
    '### 场景8',
    '### 场景9',
    '## 核心工具速览',
    '## respond_to_user 参数说明',
    '## 回复格式',
    '## 隐私与内部信息',
]


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
