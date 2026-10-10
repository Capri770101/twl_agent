"""Context selection and current-plan evidence, independent of the executor."""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger("agent.context")


def trim_history_for_llm(history: list[dict[str, Any]], char_limit: int) -> list[dict[str, Any]]:
    """保留最近完整轮次；最近一轮过长时保留全文，避免切掉约束。"""
    turns = []
    for msg in history or []:
        if not isinstance(msg, dict) or msg.get('role') not in ('user', 'assistant'):
            continue
        content = str(msg.get('content') or '')
        if not content:
            continue
        if msg['role'] == 'user' or not turns:
            turns.append([])
        turns[-1].append({'role': msg['role'], 'content': content})
    selected = []
    used = 0
    for turn in reversed(turns):
        cost = sum(len(m['content']) for m in turn)
        if char_limit > 0 and selected and used + cost > char_limit:
            break
        selected.append(turn)
        used += cost
    return [m for turn in reversed(selected) for m in turn]



def latest_plan_summary(history: list[dict[str, Any]], max_plans: int = 3) -> str:
    """取最近一次方案卡的可读摘要（供 system prompt 注入，作为权威方案数据）。

    背景（线上实测 2026-09-15）：历史消息里的 ``data``（方案结构）并不会变成模型可读的
    内容发过去，模型只能靠上一条回复的文字「回忆」方案，实测出现过把「粉康乃馨×6 +
    粉洋桔梗×5」说成「粉佳人玫瑰配粉绣球」的失真。把方案要点显式注入 prompt 后，
    模型回答方案细节与判断「要不要改方案」都有据可依。

    🔴 **定制方案优先（2026-09-20 修复，改前必读）**：
    一轮里既可能推「DIY 定制方案」（带 ``design.main_flowers``），也可能推「现成商品」
    （只有名字/价格/图片）。原实现只取「最近一条含 plans 的消息」就返回 ——
    于是**用户定稿的 DIY 方案会被随后一轮的商品推荐顶掉**：
    第 2 轮出定制方案 → 第 3 轮顺手推了几款成品（很常见，这是「先成品后定制」策略的
    自然结果）→ 第 4 轮注入的方案上下文里只剩商品，**定制方案数据彻底丢失**。
    模型只能凭上一条回复的文字回忆，实测出现：
      · 把「玫瑰×4＋蝴蝶兰×4」复述成「非洲菊×4＋向日葵×4」（张冠李戴）；
      · 用户说「给这个方案生成效果图」时**答非所问**地又推荐了一遍成品。
    现在改为**先找最近的 DIY 方案**，找不到才退回最近的商品卡。

    Args:
        history: ``load_history`` 返回的消息列表（assistant 消息可能带 ``ui``/``data``）。
        max_plans: 最多摘要几个方案（一轮可能推多款）。

    Returns:
        形如 ``「名字」（参考价 200 元；主花：康乃馨×6、洋桔梗×5；配色：粉色系）``；
        历史里没有方案卡时返回空字符串。
    """
    diy_summary = ''        # 最近一次「定制方案」摘要（优先）
    fallback_summary = ''   # 最近一次「任意方案卡」摘要（兜底）
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        data = msg.get('data')
        if not isinstance(data, dict):
            continue
        plans = data.get('plans')
        if not isinstance(plans, list) or not plans:
            continue
        diy_items: list[str] = []
        any_items: list[str] = []
        for plan in plans[:max_plans]:
            if not isinstance(plan, dict):
                continue
            name = str(plan.get('name') or '').strip()
            if not name:
                continue
            detail: list[str] = []
            price = plan.get('budget_num') or plan.get('price')
            if isinstance(price, (int, float)) and price > 0:
                detail.append(f'参考价 {price:g} 元')
            design = plan.get('design') if isinstance(plan.get('design'), dict) else {}
            flowers = design.get('main_flowers') or []
            stems = [
                f"{f.get('name')}×{f.get('qty')}"
                for f in flowers
                if isinstance(f, dict) and f.get('name')
            ]
            if stems:
                detail.append('主花：' + '、'.join(stems))
            # ⚠️ 配材/叶材也必须注入 —— 只给主花时模型会**自己编**。
            # 实测（2026-09-20）：摘要只含主花「洋桔梗×4、玫瑰×4」，模型在后续轮里
            # 凭空补出「2 支勿忘我、1 支尤加利叶」，用户拿去跟店家核料就会对不上。
            side = [
                f"{f.get('name')}×{f.get('qty')}"
                for key in ('fillers', 'foliage')
                for f in (design.get(key) or [])
                if isinstance(f, dict) and f.get('name')
            ]
            if side:
                detail.append('配材叶材：' + '、'.join(side))
            # ⚠️ color_scheme 是 list：直接 str() 会把 "['香槟', '白']" 这种 Python 字面量
            # 原样注入 prompt（脏格式，干扰模型）。
            colors = design.get('color_scheme') or []
            if isinstance(colors, list):
                colors = '、'.join(str(c) for c in colors if c)
            colors = str(colors).strip()
            if colors:
                detail.append(f'配色：{colors}')
            if str(design.get('packaging') or '').strip():
                detail.append(f"包装：{str(design['packaging']).strip()}")
            line = f'「{name}」' + (f'（{"；".join(detail)}）' if detail else '')
            any_items.append(line)
            # 有主花明细 = 定制方案（商品卡只有名字/价格，没有 design.main_flowers）
            if stems:
                diy_items.append(line)
        if not diy_summary and diy_items:
            diy_summary = '；'.join(diy_items)
        if not fallback_summary and any_items:
            fallback_summary = '；'.join(any_items)
        if diy_summary:
            break                       # 定制方案是权威数据，找到就停（取最近一版）
    result = diy_summary or fallback_summary
    if result:
        logger.info('[agent] 注入当前方案上下文（%s）',
                    '定制方案' if diy_summary else '商品卡')
    return result

