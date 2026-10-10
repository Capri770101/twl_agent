"""轻量业务意图路由：只负责明显意图，模糊消息交给完整 ReAct。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from agent.engine.tool_scope import is_standalone_care
from agent.orchestration.policies.routing import load_routing_policies


@dataclass(frozen=True)
class IntentRoute:
    name: str
    max_iterations: int
    tools: frozenset[str] | None = None


_POLICIES = load_routing_policies()


def _route(policy_name: str) -> IntentRoute:
    policy = _POLICIES[policy_name]
    return IntentRoute(policy.route_name, policy.max_iterations, policy.tools)


def classify(message: str) -> IntentRoute | None:
    text = str(message or '').strip()
    if not text:
        return None
    # “订单提醒”仍是提醒，不能先被“我的订单”分流为订单售后。
    if '提醒' in text:
        return _route('reminder')
    if any(w in text for w in ('我的订单', '订单状态', '订单进度', '退款进度', '售后进度', '查订单',
                               '取消订单', '取消这单', '退了这单', '申请退款', '不想要了', '退货退款')):
        from backend.config import settings
        if settings.CUSTOMER_SERVICE_ENABLED:
            return _route('customer_service_orders')
    if any(w in text for w in ('我的工单', '工单进度', '联系商家记录', '商家联系单', '登记的诉求', '客服事项')):
        from backend.config import settings
        if settings.CUSTOMER_SERVICE_ENABLED:
            return _route('customer_service_tickets')
    # 评价口碑：查评价为主（可带店铺信息查询）
    if any(w in text for w in ('评价', '口碑', '评分怎么样')):
        from backend.config import settings
        if settings.CUSTOMER_SERVICE_ENABLED:
            return _route('shop_service_reviews')
    # 联系商家/转人工/投诉：明确诉求直接开通行（含 contact_merchant）；
    # 商家问联系方式本身仍走 shop_service（只查信息不建工单）。
    if any(w in text for w in ('联系商家', '联系店家', '联系店铺', '转人工', '人工客服', '投诉', '催单', '帮我联系')):
        from backend.config import settings
        if settings.CUSTOMER_SERVICE_ENABLED:
            return _route('customer_service_contact')
    # “定制并出图”需要设计工具；“不要出图”不是生图意图。
    if any(w in text for w in ('营业时间', '几点关门', '几点开门', '几点营业', '店铺地址', '门店地址', '在哪地址', '配送范围', '配送规则', '能不能送', '可以配送吗', '电话多少', '联系方式')):
        from backend.config import settings
        if settings.CUSTOMER_SERVICE_ENABLED:
            return _route('shop_service_info')
    image_requested = any(w in text for w in ('效果图', '出图', '生成图', '看看成品')) and not any(w in text for w in ('不要图', '不要生图', '不要效果图', '不用效果图', '不出图', '别出图'))
    new_design = any(w in text.lower() for w in ('定制', 'diy', '自己配', '自己搭', '设计一束', '设计一个', '改成', '换成', '换个配色'))
    if image_requested and not new_design and any(w in text for w in ('这个方案', '此方案', '该方案', '给「', '给“')):
        return _route('image')
    if any(w in text for w in ('祝福', '贺卡', '寄语')) and not new_design and not any(w in text for w in ('买', '推荐', '预算')):
        return _route('greeting')
    explicit_diy = new_design or any(w in text for w in ('专属方案', '独一无二'))
    declines_diy = any(w in text.lower() for w in ('不要定制', '不用定制', '不想定制', '不要diy', '不用diy'))
    constrained_single = ('不要配花' in text or '纯' in text) and bool(re.search(r'\d+\s*(?:朵|枝|支)', text))
    if (explicit_diy and not declines_diy) or (constrained_single and not any(w in text for w in ('现成', '现货'))):
        return _route('design')
    if is_standalone_care(text):
        return _route('qa')
    if any(w in text for w in ('想买现成', '买现成', '推荐一束', '推荐花束', '有什么花', '在售')) or ('预算' in text and any(w in text for w in ('送', '推荐', '买'))):
        return _route('buying')
    return None
