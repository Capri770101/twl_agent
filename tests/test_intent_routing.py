from agent.engine.intent import classify


def test_route_care():
    route = classify('玫瑰怎么养得久？只讲养护，不要方案和图片。')
    assert route and route.name == 'qa' and route.max_iterations == 2
    assert 'retrieve_knowledge' in route.tools


def test_route_image():
    route = classify('给这个方案生成一张效果图')
    assert route and route.name == 'image' and route.max_iterations == 2
    assert route.tools == frozenset({'generate_effect_image', 'respond_to_user'})


def test_route_greeting():
    route = classify('帮我写给女朋友的生日祝福')
    assert route and route.name == 'greeting'
    assert 'suggest_greetings' in route.tools


def test_mixed_message_is_conservative():
    assert classify('预算200送妈妈，推荐一束好养的花') is None
