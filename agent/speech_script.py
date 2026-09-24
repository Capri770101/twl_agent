"""speech_script.py —— 语音播报文案生成（确定性模板，不调 LLM）。

设计原则（2026-09-24 与产品确认）：
- 语音回复**不朗读**完整方案/推荐/知识全文——那又慢又贵，用户也不想在电话里
  听一段 200 字的花材清单；
- 语音只说「结果一句话 + 下一步怎么操作」，详细内容由屏幕上的卡片承载；
- 文案由本模块按 UI 类型确定性生成（零 LLM 成本、零延迟、可测试），
  不交给模型即兴发挥，避免播报内容不可控。

输入是 ChatResponse 的 dump（dict），输出是 ≤120 字左右的口语文案。
"""
from __future__ import annotations

from typing import Any


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
    """按响应类型生成语音播报文案（引导操作，不念全文）。"""
    data = data or {}
    ui = str(ui or 'text')

    if ui == 'plan_card':
        plans = data.get('plans') or []
        diy = [p for p in plans if isinstance(p, dict) and p.get('diy')]
        shop = [p for p in plans if isinstance(p, dict) and not p.get('diy')]
        if diy:
            return f'定制方案做好了，一共{len(diy)}个。点保存可以存进你的方案，用料清单可以复制给花店确认报价。'
        if shop:
            return f'帮你挑了{len(shop)}款花束。点卡片可以看详情，喜欢就直接加购物车结算。'
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
        return '效果图正在生成，大概十几秒，好了会自动显示。'

    if ui == 'greeting_card':
        if data.get('poll') or data.get('task_id'):
            return '贺卡正在生成，稍等十几秒就能看到。'
        return '贺卡做好了，可以下载保存，或者直接用在订单里。'

    if ui == 'dialog_options':
        options = data.get('options') or []
        return f'给你{len(options) or "几"}个选项，点屏幕上的按钮告诉我就行。'

    if ui == 'order_card':
        return '订单信息已经生成，确认无误后点立即下单。'

    if ui == 'pay_jump':
        return '订单已创建，点去支付完成付款。'

    # 纯文字：知识问答/闲聊等。只念首句 + 提示看屏幕，避免长段朗读。
    head = _first_sentence(reply, limit=48)
    if not head:
        return '回复已显示在屏幕上。'
    if len(reply or '') > 60:
        return head + '详细内容已经显示在屏幕上，可以慢慢看。'
    return head
