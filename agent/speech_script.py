"""speech_script.py —— 语音播报文案生成（确定性模板，不调 LLM）。

设计原则（2026-09-28 内容摘要升级）：
- 语音回复**不朗读**完整方案/推荐/知识全文——那又慢又贵，用户也不想在电话里
  听一段 200 字的花材清单；
- 语音介绍主要内容及下一步操作：主花、配色、包装、参考价，或最多三条正文要点；
- 文案由本模块按 UI 类型确定性生成（零 LLM 成本、零延迟、可测试），
  不交给模型即兴发挥，避免播报内容不可控。

输入为响应类型、卡片数据和正文；DIY 摘要上限 280 字，不将预算冒充报价。
"""
from __future__ import annotations

from typing import Any
import re
import math


def _clean(value: Any) -> str:
    text = str(value or '')
    text = re.sub(r'https?://\S+|```[\s\S]*?```', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    return re.sub(r'\s+', ' ', re.sub(r'[*#>`|]', '', text)).strip()


def _summary(reply: str, limit: int = 150) -> str:
    """抽取前几条完整要点，避免把长句截成不完整的价格或建议。"""
    sentences = re.split(r'(?<=[。！？!?；;])|\n+', reply or '')
    out = ''
    count = 0
    for sentence in sentences:
        sentence = re.sub(r'^\s*(?:[-•]|\d+[.、])\s*', '', _clean(sentence))
        if not sentence:
            continue
        if len(out) + len(sentence) > limit:
            if not out:
                out = sentence[:limit - 1] + '…'
            break
        out += sentence if sentence[-1] in '。！？!?；;' else sentence + '。'
        count += 1
        if count == 3:
            break
    return out


def _price(plan: dict) -> str:
    value = plan.get('price')
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return f'参考价约{value:g}元'
    # 不拿预算冒充实际报价。
    return ''


def _plan_summary(plan: dict) -> str:
    design = plan.get('design') if isinstance(plan.get('design'), dict) else {}
    bits = []
    flowers = [f for f in design.get('main_flowers', []) if isinstance(f, dict) and f.get('name')]
    if flowers:
        bits.append('主花是' + '、'.join(_clean(f['name'])[:20] for f in flowers[:3]))
    colors = design.get('color_scheme')
    if isinstance(colors, list):
        colors = '、'.join(_clean(c)[:12] for c in colors[:3])
    if colors:
        bits.append(_clean(colors)[:40] + '配色')
    if design.get('packaging'):
        bits.append('搭配' + _clean(design['packaging'])[:35])
    if _price(plan):
        bits.append(_price(plan))
    return '，'.join(bits) + ('。' if bits else '')


def _first_sentence(reply: str, limit: int = 40) -> str:
    """取文字回复的首句作为播报开头（截断到 limit 字，避免念长段）。"""
    text = (reply or '').strip().replace('\n', '，')
    if not text:
        return ''
    for sep in ('。', '！', '；', '!', ';'):
        idx = text.find(sep)
        if 0 < idx <= limit:
            return text[:idx + 1]
    return text[:limit] + ('…' if len(text) > limit else '')


def build_speech_text(ui: str, data: dict[str, Any] | None, reply: str = '') -> str:
    """按响应类型生成内容摘要与简短操作提示，不念全文。"""
    data = data or {}
    ui = str(ui or 'text')

    if ui == 'customer_login':
        return '订单和售后信息需要登录后才能查询。请点击登录，回来后可以继续刚才的问题。'
    if ui == 'customer_shops':
        shops = [s for s in data.get('shops', []) if isinstance(s, dict)]
        if not shops:
            return '暂未找到门店，请补充城市或店铺名称。'
        if len(shops) > 1:
            names = '、'.join(_clean(s.get('name'))[:24] for s in shops[:3])
            return f'找到{len(shops)}家门店，包括{names}。请选择要咨询的店铺。'
        shop = shops[0]
        hours = _clean(shop.get('business_hours'))[:60]
        delivery = _clean(shop.get('delivery_policy'))[:70]
        return (_clean(shop.get('name'))[:24] + '。' + (f'营业时间是{hours}。' if hours else '营业时间尚未提供。')
                + (f'配送说明：{delivery}。' if delivery else '配送规则尚未提供，暂不能保证可送达。'))

    if ui == 'customer_orders':
        items = data.get('items') or []
        if not items:
            return '本次查询没有找到订单。可以确认账号或换一个查询范围。'
        if len(items) == 1 and isinstance(items[0].get('after_sales'), dict):
            state = items[0]['after_sales'].get('state')
            label = {'none': '暂无售后记录', 'pending_review': '等待商家审核', 'processing': '退款处理中',
                     'succeeded': '退款成功', 'failed': '退款失败', 'rejected': '审核未通过'}.get(state, '暂时无法确认')
            return f'这笔订单的售后进度是{label}。' + ('商家审核通过不代表退款已到账。' if state in ('pending_review', 'processing') else '')
        statuses = {'pending': '待支付', 'paid': '已支付待接单', 'making': '制作中', 'delivering': '配送中',
                    'completed': '已完成', 'cancelled': '已取消', 'refund_applying': '退款审核中',
                    'refunding': '退款处理中', 'refunded': '已退款', 'refund_failed': '退款失败'}
        states = '、'.join(statuses.get(p.get('status'), '状态待确认') for p in items[:3] if isinstance(p, dict))
        return f'本页查到{len(items)}笔订单，前几笔的状态是{states}。可以选择对应订单继续查询。'

    if ui == 'plan_card':
        plans = data.get('plans') or []
        diy = [p for p in plans if isinstance(p, dict) and p.get('diy')]
        shop = [p for p in plans if isinstance(p, dict) and not p.get('diy')]
        if diy:
            plan = diy[0]
            title = _clean(plan.get('name'))[:24]
            return (f'定制方案做好了，一共{len(diy)}个。' + (f'这版是{title}。' if title else '')
                    + _plan_summary(plan) + '可以保存方案，最终报价请与花店确认。')[:280]
        if shop:
            samples = '；'.join('，'.join(x for x in (_clean(p.get('name'))[:24], _price(p)) if x)
                               for p in shop[:2] if p.get('name'))
            return f'帮你挑了{len(shop)}款花束。' + (f'其中有{samples}。' if samples else '') + '喜欢的可以点卡片看详情，再加入购物车。'
        return _first_sentence(reply) + '方案已显示在屏幕上，点卡片查看详情。'

    if ui == 'shop_card':
        shops = data.get('shops') or []
        return f'为你找到{len(shops) or "几"}家花店，点卡片可以进店看看。'

    if ui == 'image_task':
        status = str(data.get('status') or '')
        if status == 'done':
            return '效果图已经生成好了，在屏幕上可以直接查看。'
        if status == 'failed':
            return '效果图生成失败了，你可以稍后再试一次。'
        return '效果图正在生成，完成后会自动显示。'

    if ui == 'greeting_card':
        if data.get('poll') or data.get('task_id'):
            return '贺卡正在生成。' + _summary(str(data.get('text') or ''), 80)
        return '贺卡做好了。' + _summary(str(data.get('text') or ''), 80) + '可以下载保存，或者用在订单里。'

    if ui == 'dialog_options':
        options = data.get('options') or []
        labels = '、'.join(_clean(o.get('label'))[:20] for o in options[:3] if isinstance(o, dict) and o.get('label'))
        return _summary(reply, 80) + (f'可以选{labels}。' if labels else '') + '点屏幕上的按钮告诉我就行。'

    if ui == 'order_card':
        return '订单信息已经生成，确认无误后点立即下单。'

    if ui == 'pay_jump':
        return '订单已创建，点去支付完成付款。'

    # 纯文字提取最多三条要点；这是抽取式摘要，不新增模型调用。
    head = _summary(reply)
    if not head:
        return '回复已显示在屏幕上。'
    if len(_clean(reply)) > len(head):
        return head + '详细内容已经显示在屏幕上，可以慢慢看。'
    return head
