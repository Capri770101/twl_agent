"""方案上下文注入回归门：历史方案卡 → system prompt 中的「权威方案」摘要。

背景：历史消息的 ``data``（方案结构）不会作为可读内容发给模型，模型只能靠上一条回复的
文字「回忆」方案——线上实测出现过把「粉康乃馨×6 + 粉洋桔梗×5」说成「粉佳人玫瑰配粉绣球」
的失真。本文件锁住「方案要点必须被显式注入」这一行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.agent import ReActAgent, _latest_plan_summary


def test_current_plan_survives_history_window(monkeypatch):
    import asyncio
    import agent.agent as agent_module

    async def saved_plan(user_id, session_id, key):
        assert (user_id, session_id, key) == ('u', 's', 'latest_diy_plan')
        return {'name': '最新定制', 'design': {'main_flowers': [{'name': '玫瑰', 'qty': 9}]}}

    monkeypatch.setattr(agent_module.mem_store, 'get_session_json', saved_plan)
    summary = asyncio.run(agent_module._current_plan_summary('u', 's', [_product_card_msg(('旧商品', 100))]))
    assert '最新定制' in summary and '玫瑰×9' in summary
    assert '旧商品' not in summary


def test_current_plan_falls_back_to_history(monkeypatch):
    import asyncio
    import agent.agent as agent_module

    async def missing(*args):
        return None

    monkeypatch.setattr(agent_module.mem_store, 'get_session_json', missing)
    summary = asyncio.run(agent_module._current_plan_summary('u', 's', [_product_card_msg(('当前商品', 100))]))
    assert '当前商品' in summary


def _plan_msg(name: str, price: float, flowers: list[dict], colors: list | str) -> dict:
    return {
        'role': 'assistant',
        'content': '方案已生成',
        'ui': 'plan_card',
        'data': {'plans': [{
            'name': name,
            'budget_num': price,
            'design': {'main_flowers': flowers, 'color_scheme': colors},
        }]},
    }


# ── _latest_plan_summary ──

def test_no_history_returns_empty():
    assert _latest_plan_summary([]) == ''
    assert _latest_plan_summary(None) == ''  # type: ignore[arg-type]


def test_text_message_returns_empty():
    hist = [{'role': 'assistant', 'content': '你好', 'ui': 'text', 'data': {}}]
    assert _latest_plan_summary(hist) == ''


def test_summary_contains_key_facts():
    s = _latest_plan_summary([_plan_msg(
        '粉韵温情', 200,
        [{'name': '康乃馨', 'qty': 6}, {'name': '洋桔梗', 'qty': 5}],
        '粉色系',
    )])
    assert '粉韵温情' in s
    assert '200' in s
    assert '康乃馨×6' in s and '洋桔梗×5' in s
    assert '粉色系' in s


def test_latest_plan_wins():
    """多轮改方案后，注入的必须是**最新**那版，否则模型会拿着旧方案回答。"""
    older = _plan_msg('旧方案', 100, [{'name': '玫瑰', 'qty': 11}], '红色系')
    newer = _plan_msg('新方案', 300, [{'name': '绣球', 'qty': 3}], '白色系')
    s = _latest_plan_summary([older, {'role': 'user', 'content': '换一个'}, newer])
    assert '新方案' in s and '旧方案' not in s


def test_malformed_plans_are_safe():
    hist = [{'role': 'assistant', 'content': '', 'data': {'plans': [None, {}, {'name': ''}, 'x']}}]
    assert _latest_plan_summary(hist) == ''


def test_multi_plan_card_summarized():
    hist = [{
        'role': 'assistant', 'content': '', 'ui': 'plan_card',
        'data': {'plans': [
            {'name': 'A', 'budget_num': 100, 'design': {}},
            {'name': 'B', 'budget_num': 200, 'design': {}},
        ]},
    }]
    s = _latest_plan_summary(hist)
    assert 'A' in s and 'B' in s


# ── _build_system 注入 ──

def test_build_system_injects_plan():
    out = ReActAgent._build_system(  # type: ignore[arg-type]
        None, None, {}, current_plan='「粉韵温情」（参考价 200 元；主花：康乃馨×6）',
    )
    assert '当前方案' in out
    assert '康乃馨×6' in out
    assert 'revise_diy_plan' in out  # 改方案必须调工具，而不是只写文字


def test_build_system_omits_plan_when_empty():
    out = ReActAgent._build_system(None, None, {}, current_plan='')  # type: ignore[arg-type]
    assert '会话中的当前方案' not in out


# ── 定制方案 vs 商品卡：定制方案优先（2026-09-20 修复）──

def _product_card_msg(*items: tuple[str, float]) -> dict:
    """现成商品卡：只有名字 / 价格，**没有 ``design.main_flowers``** —— 这是它与定制方案的区别。"""
    return {
        'role': 'assistant',
        'content': '给你挑了几款现成的',
        'ui': 'plan_card',
        'data': {'plans': [{'name': n, 'price': p} for n, p in items]},
    }


def test_diy_plan_not_replaced_by_later_product_card():
    """🔴 回归：模型在定制方案之后又推了现成商品时，**定制方案不能被顶掉**。

    线上真实链路（2026-09-20）：
      第 2 轮出 DIY 定制方案 → 第 3 轮顺手推了几款成品（「先成品后定制」策略的自然结果）
      → 第 4 轮注入的方案上下文里**只剩商品**，定制方案数据彻底丢失。
    后果：模型只能靠上一条回复的文字回忆，把「玫瑰×4＋蝴蝶兰×4」复述成
    「非洲菊×4＋向日葵×4」；用户说「给这个方案生成效果图」时答非所问地又推荐了一遍成品。
    """
    diy = _plan_msg('静默温柔', 178,
                    [{'name': '洋桔梗', 'qty': 2}, {'name': '玫瑰', 'qty': 2}], ['粉', '白'])
    products = _product_card_msg(('宫崎骏的夏天', 108), ('四季予你', 88))
    s = _latest_plan_summary([diy, {'role': 'user', 'content': '就这一版了'}, products])
    assert '静默温柔' in s, '定制方案被后续的商品卡顶掉了'
    assert '洋桔梗×2' in s and '玫瑰×2' in s
    assert '宫崎骏的夏天' not in s, '商品卡不该挤掉定制方案'


def test_newer_diy_plan_still_wins():
    """多版定制方案之间仍取**最新**那版（别被「优先定制」改坏）。"""
    old = _plan_msg('旧定制', 100, [{'name': '玫瑰', 'qty': 11}], ['红'])
    new = _plan_msg('新定制', 300, [{'name': '绣球', 'qty': 3}], ['白'])
    s = _latest_plan_summary([old, new, _product_card_msg(('某成品', 99))])
    assert '新定制' in s and '旧定制' not in s


def test_falls_back_to_product_card_when_no_diy():
    """会话里没有定制方案时，商品卡仍要注入（否则模型不知道推过什么）。"""
    s = _latest_plan_summary([_product_card_msg(('宫崎骏的夏天', 108))])
    assert '宫崎骏的夏天' in s


def test_colors_list_rendered_cleanly():
    """⚠️ ``color_scheme`` 是 list：注入 prompt 必须是「香槟、白」，
    不能是 ``"['香槟', '白']"``（Python 字面量会干扰模型理解）。"""
    s = _latest_plan_summary([_plan_msg('测试', 100, [{'name': '玫瑰', 'qty': 5}], ['香槟', '白'])])
    assert "['" not in s and '"' not in s
    assert '香槟、白' in s


def test_summary_includes_fillers_foliage_and_packaging():
    """⚠️ 配材 / 叶材 / 包装也要注入 —— 只给主花时模型会**自己编**。

    实测（2026-09-20）：摘要只含主花「洋桔梗×4、玫瑰×4」时，模型在后续轮里凭空补出
    「2 支勿忘我、1 支尤加利叶」，用户拿这份清单去跟店家核料就会对不上
    （DIY 方案是要交给店家照做的，用料错 = 做错花）。
    """
    hist = [{
        'role': 'assistant', 'content': '', 'ui': 'plan_card',
        'data': {'plans': [{
            'name': '素心致歉', 'price': 99,
            'design': {
                'main_flowers': [{'name': '洋桔梗', 'qty': 4}],
                'fillers': [{'name': '勿忘我', 'qty': 2}],
                'foliage': [{'name': '尤加利', 'qty': 1}],
                'color_scheme': ['浅紫', '香槟'],
                'packaging': '雾面纸',
            },
        }]},
    }]
    s = _latest_plan_summary(hist)
    assert '洋桔梗×4' in s
    assert '勿忘我×2' in s and '尤加利×1' in s, f'配材/叶材没注入：{s}'
    assert '雾面纸' in s
