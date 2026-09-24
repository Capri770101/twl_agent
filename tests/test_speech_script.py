"""speech_script 播报文案模板测试：语音只念引导，不念全文。"""
from agent.speech_script import build_speech_text


def test_diy_plan_guides_save_and_inquiry():
    text = build_speech_text('plan_card', {'plans': [{'diy': True, 'name': 'x'}]}, '')
    assert '保存' in text
    assert '花店' in text or '报价' in text


def test_shop_plans_guide_checkout():
    text = build_speech_text('plan_card', {'plans': [{'diy': False}, {'diy': False}]}, '')
    assert '2' in text
    assert '购物车' in text or '结算' in text


def test_image_task_states():
    assert '生成' in build_speech_text('image_task', {'status': 'processing'}, '')
    assert '屏幕上' in build_speech_text('image_task', {'status': 'done'}, '')
    assert '失败' in build_speech_text('image_task', {'status': 'failed'}, '')


def test_greeting_card_pending_vs_done():
    assert '正在生成' in build_speech_text('greeting_card', {'task_id': 't1', 'poll': '/tasks/t1'}, '')
    done = build_speech_text('greeting_card', {'image_url': '/generated/x.png'}, '')
    assert '下载' in done or '订单' in done


def test_dialog_options_and_order_and_pay():
    assert '按钮' in build_speech_text('dialog_options', {'options': [{'label': 'a'}, {'label': 'b'}]}, '')
    assert '下单' in build_speech_text('order_card', {}, '')
    assert '支付' in build_speech_text('pay_jump', {}, '')


def test_long_text_reply_only_reads_first_sentence():
    reply = '玫瑰花期五到七天。' + '详细养护' * 60
    text = build_speech_text('text', {}, reply)
    assert text.startswith('玫瑰花期五到七天。')
    assert len(text) < 80
    assert '屏幕上' in text


def test_short_text_reply_read_whole():
    assert build_speech_text('text', {}, '好的，马上帮你查。') == '好的，马上帮你查。'


def test_empty_reply_fallback():
    assert build_speech_text('text', {}, '') == '回复已显示在屏幕上。'
