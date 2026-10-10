"""Regression checks for care routing and scoped prompt guidance."""
from pathlib import Path

import pytest

from agent.engine.intent import classify
from agent.engine.tool_scope import CARE_TOOLS, is_standalone_care


@pytest.mark.parametrize('message', [
    '玫瑰怎么养？', '鲜花如何养', '百合养护', '多久换一次水？',
    '换一次水后怎么养？', '鲜花剪根', '怎么保鲜', '如何醒花',
    '玫瑰怎么养得久？只讲养护，不要方案和图片。',
])
def test_care_route_matches_standalone_scope(message):
    assert is_standalone_care(message)
    route = classify(message)
    assert route is not None and route.name == 'qa'
    assert route.tools == CARE_TOOLS


@pytest.mark.parametrize('message', [
    '推荐好养的花', '给妈妈买花怎么养', '预算200怎么养玫瑰',
    '这个方案怎么养', '这个配色怎么养', '定制一束花再讲养护',
    '我的订单和玫瑰养护', '这家店如何养花', '看看价格和养护',
    '出图并讲养护', '换成白色再讲养护', '改成蓝色再讲养护',
    '做贺卡并讲养护', '记住我喜欢玫瑰并讲养护',
])
def test_mixed_request_never_gets_care_only_scope(message):
    assert not is_standalone_care(message)
    route = classify(message)
    assert route is None or route.name != 'qa'


def test_prompt_customer_queries_and_experience_precedence():
    prompts = Path(__file__).resolve().parent.parent / 'agent' / 'prompts'
    base = (prompts / 'base.md').read_text(encoding='utf-8')
    experience = (prompts / 'full_platform_plan_only.md').read_text(encoding='utf-8')
    assert 'entity="order"' not in base
    assert '| 问本人订单状态 | `query_my_orders`' in base
    assert '| 问某家店营业 / 配送 / 起送价 / 地址 | `query_shop_service`' in base
    assert '模式提示优先于本文件任何冲突的规则或示例' in base
    assert '本模式约束覆盖 `base.md`' in experience
