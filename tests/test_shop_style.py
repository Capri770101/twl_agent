from agent.shop_style import build_style_profile, style_prompt_suffix


def test_style_profile_extracts_recent_shop_signals():
    profile = build_style_profile([
        {'name': '白绿韩式花束', 'tags': ['自然', '奶油'], 'flowers': '白玫瑰 尤加利', 'packaging': '雾面纸'},
        {'name': '香槟轻奢礼盒', 'description': '高级留白', 'packaging': '礼盒'},
    ], 'shop-1')
    assert '韩式' in profile['styles']
    assert '白' in profile['colors'] and '绿' in profile['colors']
    assert '雾面纸' in profile['packaging']
    assert '参考' in style_prompt_suffix(profile)
    assert '不得把参考商品的花材替换' in style_prompt_suffix(profile)


def test_empty_profile_is_safe():
    assert style_prompt_suffix({}) == ''
