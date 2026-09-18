"""agent.py —— 智能体主类：ReAct 主循环 + 会话状态机驱动。

核心职责：
1. 载入短期记忆（历史消息）+ 长期记忆（用户偏好），拼成 system prompt。
2. 进入「思考-行动-观察」循环：call_llm → 解析工具调用 → 执行 → 回填 → 再思考，
   直到模型给出最终回复或达到 max_iterations。
3. 根据本轮工具产出推导 UI 焦点（focus，仅前端高亮）并产出结构化 UI（plan_card / shop_card / pay_jump ...）。
   流程不再由状态机硬锁，用户可随时调用任一 skill（设计/改设计/生图/看店/下单）。
4. 最终根据本轮工具产出结构化 UI（plan_card / shop_card / pay_jump ...）。

说明：
- call_llm 为 OpenAI 兼容真实接口（live-only），必须配置 LLM_API_KEY，已弃用 Mock 引擎。
- 同步存储操作通过 asyncio.to_thread 调用，避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from string import Template
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from agent.engine.llm import call_llm, call_llm_stream
from agent.engine.state import SessionStage
from agent.engine.ui_protocol import AgentAction, AgentActionType, ChatResponse, ToolCallRecord, UIType
from agent.ports import normalize_entry, normalize_product_id, normalize_product_title, normalize_shop_id
from agent.toolkit import allowed_entities, execute_tool, to_openai_tools
from backend.config import settings, setup_logging
from backend.storage import memory as mem_store

_CHITCHAT_WORDS = ('你好', '您好', '在吗', '在么', '嗨', '哈喽', '谢谢', '感谢', '再见', '拜拜', '哈哈', '辛苦了', '赞', '呵呵')

# 单轮时间预算（P0 防「线程裸跑」）：
# 调用方（/chat、/chat/stream）在 REQUEST_TIMEOUT 处等待，但 asyncio 超时**只能取消 await**，
# 杀不掉 run_in_executor 的线程。故 run() 自己也要感知 deadline，预算耗尽时主动收口，
# 而不是让线程「慢 LLM × 多轮 × 重试」一路裸跑到十几分钟（并继续写状态 / 烧 token）。
_DEADLINE_MARGIN = 5.0   # 给后处理 / 落库留的余量（秒）
_MIN_ITER_BUDGET = 8.0   # 剩余预算低于此值就不再开新一轮 LLM 调用

# 智能体专用线程池（P2）：与 asyncio 默认线程池隔离，避免「慢轮次占满全局默认池」而拖垮
# 其它请求（默认池还被记忆固化等其它 to_thread 任务共用）。并发上限与 /chat 的并发护栏同源。
_AGENT_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, settings.AGENT_MAX_CONCURRENCY), thread_name_prefix='flora-agent')

# 分隔线行（--- / *** / —— / ___ / === 及其带空格变体）：用户反馈这类分段排版难看，
# 要求改为数字编号分点。prompt 已加规则，这里再兜一道 deterministic 清理。
_SEPARATOR_LINE = re.compile(r'^[\s]*([-—*_=＝]{1}[\s]*){2,}$')


def _strip_separator_lines(text: str) -> str:
    """删掉回复里独立的分隔线行（如 ``---``、``***``、``——``），并收拢多余空行。

    Args:
        text: LLM 生成的最终回复文本。

    Returns:
        清理后的文本；非字符串或空文本原样返回。
    """
    if not isinstance(text, str) or not text:
        return text
    kept = [ln for ln in text.split('\n') if not _SEPARATOR_LINE.match(ln)]
    out = re.sub(r'\n{3,}', '\n\n', '\n'.join(kept))
    return out.strip('\n')


# 内部实现标识 → 中性说法（隐私约束的 deterministic 兜底）。
# prompt 已硬约束「不暴露工具名 / 内部字段」，但 prompt 是软约束，此处再兜一层：
# 模型偶尔仍会把工具名或内部字段写进回复。
_INTERNAL_TERM_SUBS = (
    (re.compile(r'platform_(?:db|mapping)_[a-z_]+', re.I), '平台查询'),
    (re.compile(r'PLATFORM_DB_[A-Z0-9_]+'), '平台数据源'),
    (re.compile(r'\bsource_id\b', re.I), '数据源'),
)

# 数据库结构 / 表名 / 字段名 / SQL → 中性说法。
#
# Capri 2026-09-16 要求：「不能把数据库的内容如数据表等信息全盘托出」。
# prompt 已硬约束（见 base.md 隐私段），但 prompt 是软约束——这里再兜一层**确定性**替换，
# 只兜「明显是库内标识」的词：内部表名、平台原始字段名、SQL、连接串、迁移文件。
# 业务词（商品 / 店铺 / 价格 / 营业时间）不受影响，用户该看到的结论照常给。
_DB_SCHEMA_SUBS = (
    # 内部表名（模型若复述表结构会命中）
    (re.compile(r'\b(?:products|shops|orders|categories|banners|coupons|feedback|members|stats'
                r'|messages|sessions|diy_plans|image_tasks|user_preferences|proven_plans)\b', re.I),
     '平台数据'),
    # 平台原始字段名（驼峰 / 下划线两种写法）
    (re.compile(r'\b(?:ownerShopId|planId|shopId|productId|categoryId|flowerMeaning|subMchId'
                r'|profitSharingRatio|originalPrice|businessHours|minOrderPrice|deliveryFee'
                r'|deliveryTime|monthSales|shelfLife|ratingCount|openStatusText|isOpenNow'
                r'|owner_shop_id|shop_id|plan_id|product_id|category_id|flower_meaning'
                r'|original_price|business_hours|min_order_price|delivery_fee|delivery_time'
                r'|month_sales|shelf_life|rating_count|open_status_text|is_open_now)\b'),
     '相关字段'),
    # SQL 语句：整句抹掉，避免泄露查询语义
    (re.compile(r'\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\b[^。！？\n]*', re.I), ''),
    # 连接串 / 迁移文件
    (re.compile(r'(?:postgres(?:ql)?|mysql|redis)://\S+', re.I), '(内部地址)'),
    (re.compile(r'\bPLATFORM_(?:DB|API)_[A-Z0-9_]+\b'), '平台数据源'),
    (re.compile(r'\b\d{3}_[a-z_]+\.sql\b|\bmigrations\b', re.I), '内部资料'),
)

# 整行看起来就是工具的原始返回（JSON 对象 / 数组）：正常花艺回复不会出现。
# 对应「不要把数据库查询结果输出到前端」的兜底——只删独立数据行，不碰正文。
_RAW_PAYLOAD_LINE = re.compile(r'^\s*[\{\[].*[\}\]]\s*$')


def _strip_internal_leak(text: str) -> str:
    """移除回复里泄露内部实现 / 原始查询结果的内容（隐私约束兜底）。

    Args:
        text: LLM 生成的最终回复文本。

    Returns:
        清理后的文本；非字符串或空文本原样返回。
    """
    if not isinstance(text, str) or not text:
        return text
    kept: list[str] = []
    dropped_payload = False
    for ln in text.split('\n'):
        if _RAW_PAYLOAD_LINE.match(ln):
            dropped_payload = True
            continue
        kept.append(ln)
    out = '\n'.join(kept)
    for pat, repl in _INTERNAL_TERM_SUBS:
        out = pat.sub(repl, out)
    # 数据库结构兜底：表名 / 字段名 / SQL / 连接串 → 中性说法
    # （Capri 要求：不能把数据库内容全盘托出；prompt 是软约束，这里确定性收口）
    schema_hit = False
    for pat, repl in _DB_SCHEMA_SUBS:
        out, _n = pat.subn(repl, out)
        if _n:
            schema_hit = True
    out = re.sub(r'\n{3,}', '\n\n', out).strip('\n')
    if dropped_payload:
        logger.warning('[agent] 回复中出现原始数据行，已移除（隐私兜底）')
    if schema_hit:
        logger.warning('[agent] 回复中出现数据库结构相关标识，已替换为中性说法（隐私兜底）')
    return out


# 危险 HTML 标签（会把用户输入变成可执行内容的那一类）。
# 只列「明确危险」的标签名，不做全量转义 —— 见 _neutralize_dangerous_tags 的说明。
_DANGEROUS_TAG_RE = re.compile(
    r'<\s*/?\s*(?:script|iframe|frame|frameset|object|embed|applet|svg|math|link|style'
    r'|base|meta|form|input|button|textarea|select|video|audio|source|track'
    r'|details|marquee|template|noscript|img|image|body|html|head|a)\b[^>]*>',
    re.I,
)


def _neutralize_dangerous_tags(text: str) -> str:
    """把回复里可能被当成 HTML 渲染的**危险标签**转义掉（XSS 兜底）。

    为什么（2026-09-18 外部安全审计 P0）：`reply` 是模型输出的**纯文本**，服务端此前
    不做任何处理，安全责任被推给了每一个接入方 —— 演示页做了转义所以安全，但
    **任何不转义的第三方接入端**都会把 `<script>` 原样渲染；若该端还把消息存库回显，
    就变成存储型 XSS。

    刻意**只中和明确危险的标签名**，不做全量转义：全量转义会把正常文本里的 `<`、`>`
    变成 `&lt;`、`&gt;`（如「我<3你」），在纯文本端反而显示异常。转义后标签失去语义，
    但**文字内容仍然可读**（用户看到的是「&lt;script&gt;」而不是标签生效）。

    Args:
        text: 模型生成的回复文本。

    Returns:
        危险标签已转义的文本；无危险标签、非字符串或空文本时原样返回。
    """
    if not isinstance(text, str) or not text:
        return text
    out, n = _DANGEROUS_TAG_RE.subn(
        lambda m: m.group(0).replace('<', '&lt;').replace('>', '&gt;'),
        text,
    )
    if n:
        logger.warning('[agent] 回复中出现危险 HTML 标签，已转义（XSS 兜底，共 %d 处）', n)
    return out


# 卡片类回复的「要点兜底」阈值：reply 短于此长度、且本轮带卡片时，才自动补充要点。
#
# 背景：实测模型在带卡片时强烈倾向只回「已推给你，点卡片看」而不给结论。已尝试 3 处
# prompt 强化（回复格式 / 全平台模式 / 场景6），效果不稳定（生成有随机性）。prompt 是
# 软约束，这里补一层**确定性**兜底：从卡片数据取可读字段拼成简短要点。
# 只追加、不改写模型原话；只在 reply 明显过短时触发，避免重复啰嗦。
_CARD_SUMMARY_MIN_REPLY = 30


def _ensure_card_summary(reply: str, ui: UIType, data: dict[str, Any]) -> str:
    """卡片类回复过短时，追加一句基于卡片数据的可读要点（不泄露内部标识）。

    Args:
        reply: 模型给出的文字回复。
        ui: 本轮 UI 类型。
        data: 卡片数据（plan_card 取 plans，shop_card 取 shops）。

    Returns:
        可能已追加要点的回复；非卡片类型、reply 足够长或无可用字段时原样返回。
    """
    if ui not in (UIType.PLAN_CARD, UIType.SHOP_CARD, UIType.ORDER_CARD, UIType.PAY_JUMP, UIType.IMAGE_TASK, UIType.GREETING_CARD):
        return reply
    if len((reply or '').strip()) >= _CARD_SUMMARY_MIN_REPLY:
        return reply
    base = (reply or '').strip()

    # 需求 / 结果类卡片：正文在卡片里，短回复只需一句通用引导（不必读 data）。
    # 修「卡片有、文字空」——此前只兜 plan/shop，order/pay/image/greeting 会留下空回复。
    _simple = {
        UIType.ORDER_CARD: '订单信息已生成，确认无误就可以去支付啦～',
        UIType.PAY_JUMP: '订单信息已生成，确认无误就可以去支付啦～',
        UIType.IMAGE_TASK: '效果图正在生成中，稍等一下就好～',
        UIType.GREETING_CARD: '贺卡已经做好啦，看看喜欢吗？',
    }
    if ui in _simple:
        logger.info('[agent] 卡片回复过短，已自动追加要点（%s）', ui.value)
        return f'{base}\n\n{_simple[ui]}' if base else _simple[ui]

    if not isinstance(data, dict):
        return reply

    lines: list[str] = []
    if ui == UIType.PLAN_CARD:
        for plan in (data.get('plans') or [])[:4]:
            name = str((plan or {}).get('name') or '').strip()
            if not name:
                continue
            price = (plan or {}).get('price')
            lines.append(f'- {name}（{price:g} 元）' if isinstance(price, (int, float)) and price > 0 else f'- {name}')
        head = '给你挑的是这几款：'
    else:
        for shop in (data.get('shops') or [])[:4]:
            name = str((shop or {}).get('name') or '').strip()
            if not name:
                continue
            bits: list[str] = []
            rating = (shop or {}).get('rating')
            if isinstance(rating, (int, float)) and rating > 0:
                bits.append(f'{rating:g} 分')
            price_range = str((shop or {}).get('price_range') or '').strip()
            if price_range:
                bits.append(price_range)
            lines.append(f'- {name}' + (f'（{"、".join(bits)}）' if bits else ''))
        head = '为你筛选了这几家：'

    if not lines:
        return reply
    summary = head + '\n' + '\n'.join(lines)
    base = (reply or '').strip()
    logger.info('[agent] 卡片回复过短，已自动追加要点（%d 条）', len(lines))
    return f'{base}\n\n{summary}' if base else summary


# 纯文本回复被清空时的确定性兜底。
#
# 背景（线上实测 2026-09-15）：模型某轮把原始数据行当成正文输出 → `_strip_internal_leak`
# 把整段删空；而它在清理链里**排在 `_ensure_card_summary` 之前**，且后者只兜卡片类 UI
# → 纯文本场景下整条回复变成空串，用户只看到一个空气泡（表现为「答非所问 / 没回答」）。
# 这里补最后一道闸：清理链跑完仍为空时，一定给出一句可读的兜底，不让空回复出到前端。
_EMPTY_TEXT_FALLBACK = '抱歉，刚才那句我组织得不太清楚。你再说一下想要的风格或用途，我马上给你配一束～'
_EMPTY_CARD_FALLBACK = '我已经为你整理好相关结果啦，请查看下方卡片～'

_CARD_UIS = (
    UIType.PLAN_CARD, UIType.SHOP_CARD, UIType.ORDER_CARD,
    UIType.PAY_JUMP, UIType.IMAGE_TASK, UIType.GREETING_CARD,
)


def _ensure_non_empty_reply(reply: str, ui: UIType) -> str:
    """清理链跑完后仍为空 → 给确定性兜底，杜绝空回复。

    Args:
        reply: 经 ``_strip_separator_lines`` / ``_strip_internal_leak`` /
            ``_ensure_card_summary`` 处理后的回复。
        ui: 本轮 UI 类型。

    Returns:
        非空回复；原本已有内容时原样返回。
    """
    if (reply or '').strip():
        return reply
    logger.warning('[agent] 清理链后回复为空（ui=%s），已使用确定性兜底', ui.value)
    return _EMPTY_CARD_FALLBACK if ui in _CARD_UIS else _EMPTY_TEXT_FALLBACK


def _align_card_data_with_reply(ui: UIType, data: dict, reply: str) -> dict:
    """最终回复定型后，把**商品卡**与文案对齐（DIY 方案卡原样保留）。

    Args:
        ui: 本轮 UI 类型。
        data: 卡片数据。
        reply: 已定型的最终回复。

    Returns:
        对齐后的卡片数据；无需调整时原样返回。

    为什么必须放在这里、而不是 ``_derive_ui`` 内部：``_derive_ui`` 在
    ``respond_to_user`` 的 ``reply`` 覆盖 ``final_reply`` **之前**被调用，那一刻
    回复里还没有商品名，按文案过滤会**静默失效**。线上实测（2026-09-16）：
    文案推荐「感恩母亲 / 春风暖阳 / 温柔以待…」5 款，卡片却给了 8 款文案没提过的商品。
    """
    if ui != UIType.PLAN_CARD or not isinstance(data, dict):
        return data
    plans = data.get('plans')
    if not isinstance(plans, list) or not plans:
        return data
    diy = [p for p in plans if isinstance(p, dict) and (p.get('diy') is True or p.get('design'))]
    prods = [p for p in plans if isinstance(p, dict) and not (p.get('diy') is True or p.get('design'))]
    if not prods:
        return data
    aligned = ReActAgent._align_products_with_reply(prods, reply)
    if not aligned or aligned == prods:
        # 对齐不生效时把上下文打出来：大多数情况是 reply 为空/未点名（那时本就该保留全集）。
        logger.info(
            '[agent] 商品卡对齐无变化：%d 款（reply %d 字，点名命中 %d 款）',
            len(prods), len(reply or ''),
            len([r for r in prods if str(r.get('name') or '').strip() in (reply or '')]),
        )
        return data
    logger.info('[agent] 商品卡已与回复对齐：%d 款 → %d 款（reply %d 字）',
                len(prods), len(aligned), len(reply or ''))
    out = dict(data)
    out['plans'] = diy + aligned
    return out


def _shop_entity_enabled() -> bool:
    """本实例是否开放「店铺查询」链路。

    体验/演示实例把 ``PLATFORM_ALLOWED_ENTITIES`` 设为 ``plan``，于是：schema 里不暴露
    ``shop``（模型看不到这个选项）、执行层直接拒绝、prompt 换成「只做方案与建议」的变体、
    也不产出店铺卡。留空 = 不限制（生产默认）。

    Returns:
        True 表示允许店铺查询与店铺卡。
    """
    try:
        allowed = allowed_entities()
    except Exception:
        return True
    return not allowed or 'shop' in allowed


# 交易引导词：体验版不承接下单，回复里出现这些就是越界。
_TRADE_WORDS_RE = re.compile(r'下单|购买|支付|结算|发货')


def _demo_trade_note(reply: str) -> str:
    """体验版兜底：回复里出现交易引导时补一句边界说明。

    为什么不能只靠 prompt（实测 2026-09-16）：体验版实测回复结尾仍写了
    「点击卡片即可在小程序下单配送」——本项目反复验证过「模型行为类缺陷光靠 prompt 不够」。
    这里**不改写原文**（避免破坏语感），只在末尾补一句确定性说明；已经说明过就不重复。

    Args:
        reply: 最终回复。

    Returns:
        可能追加了边界说明的回复。
    """
    text = (reply or '').rstrip()
    if not text or not _TRADE_WORDS_RE.search(text) or '体验版' in text:
        return reply
    return text + '\n\n（体验版仅作参考展示，选购与下单请到正式小程序。）'


# ── system prompt 模板 ──────────────────────────────────────────────────────
# 文本统一放 agent/prompts/*.md（外置原因：prompt 是本项目改动最频繁的资产，
# 内联在 _build_system 里时改一句话要动 Python、无法单独 diff 或做 A/B）。
# 条件编排仍留在 Python，保持可读、可测。
#
# 拼接约定：md 的**每一行**对应 system prompt 的一个片段，行间用空行（"\n\n"）连接——
# 与原 `parts` 列表 + `'\n\n'.join(parts)` 的历史格式完全一致，确保搬迁零变化。
#
# 占位符用 string.Template 的 `$name` 而非 str.format 的 `{name}`：
# prompt 正文里大量出现 {plans:[...]}、{shops:[...]} 这类花括号，format 会误解析。
_PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')
_PROMPT_CACHE: dict[str, str] = {}


_RAW_CACHE: dict[str, str] = {}


def _read_raw_prompt(name: str) -> str:
    """读取模板原文（未拼接），结果缓存。"""
    if name not in _RAW_CACHE:
        path = os.path.join(_PROMPT_DIR, f'{name}.md')
        with open(path, encoding='utf-8') as handle:
            _RAW_CACHE[name] = handle.read().strip('\n')
    return _RAW_CACHE[name]


def _latest_plan_summary(history: list[dict[str, Any]], max_plans: int = 3) -> str:
    """取最近一次方案卡的可读摘要（供 system prompt 注入，作为权威方案数据）。

    背景（线上实测 2026-09-15）：历史消息里的 ``data``（方案结构）并不会变成模型可读的
    内容发过去，模型只能靠上一条回复的文字「回忆」方案，实测出现过把「粉康乃馨×6 +
    粉洋桔梗×5」说成「粉佳人玫瑰配粉绣球」的失真。把方案要点显式注入 prompt 后，
    模型回答方案细节与判断「要不要改方案」都有据可依。

    Args:
        history: ``load_history`` 返回的消息列表（assistant 消息可能带 ``ui``/``data``）。
        max_plans: 最多摘要几个方案（一轮可能推多款）。

    Returns:
        形如 ``「名字」（参考价 200 元；主花：康乃馨×6、洋桔梗×5；配色：粉色系）``；
        历史里没有方案卡时返回空字符串。
    """
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        data = msg.get('data')
        if not isinstance(data, dict):
            continue
        plans = data.get('plans')
        if not isinstance(plans, list) or not plans:
            continue
        items: list[str] = []
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
            colors = str(design.get('color_scheme') or '').strip()
            if colors:
                detail.append(f'配色：{colors}')
            items.append(f'「{name}」' + (f'（{"；".join(detail)}）' if detail else ''))
        if items:
            logger.info('[agent] 注入当前方案上下文（%d 个）', len(items))
            return '；'.join(items)
    return ''


def _load_prompt(name: str) -> str:
    """读取**无变量**模板，按「每行一片段」拼好（行间空行）并缓存。"""
    key = f'{name}::plain'
    if key not in _PROMPT_CACHE:
        _PROMPT_CACHE[key] = '\n\n'.join(_read_raw_prompt(name).split('\n'))
    return _PROMPT_CACHE[key]


def _render_prompt(name: str, **values: str) -> str:
    """读取模板、替换 ``$变量``，再按「每行一片段」约定拼回（行间空行）。

    Args:
        name: 模板名（agent/prompts/<name>.md）。
        **values: 模板里的 ``$变量`` 取值。

    Returns:
        已按 system prompt 格式拼接好的文本。
    """
    text = Template(_read_raw_prompt(name)).substitute(**values)
    return '\n\n'.join(text.split('\n'))


def _now_context() -> str:
    """当前本地时间上下文，供 LLM 判断营业时段 / 配送时效。

    平台库只存静态营业时段（如 ``business_hours='07:00-22:00'``），智能体要回答
    「现在开门吗」「还来得及送吗」就必须知道当前时间。此前 prompt 无任何时间注入，
    模型只能凭常识猜测，容易给出与营业时段矛盾的答复。
    """
    # 显式按北京时间（UTC+8）取，不依赖容器 TZ —— 容器默认 UTC 会比北京慢 8 小时，
    # 拿 UTC 时间比对 07:00-22:00 这类营业时段会得出错误结论。
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    week_cn = '一二三四五六日'[now.weekday()]
    return f'当前时间：{now:%Y-%m-%d} 星期{week_cn} {now:%H:%M}（北京时间 UTC+8，24 小时制）'

def _clean_reply(text: str) -> str:
    """清理智能体回复里的 markdown 噪声，让前端纯文本渲染更整洁。

    前端不渲染 markdown，因此 ``**加粗**`` 会原样显示成 ``**``；这里统一去除
    ``**`` 与行首 ``#`` 标题符，并把连续空行折叠为单空行，保留有序列表等可读结构。
    """
    if not text:
        return text
    text = text.replace('**', '')
    text = re.sub('(?m)^#{1,6}\\s*', '', text)
    text = re.sub('\\n{3,}', '\n\n', text)
    return text.strip()

def _is_chitchat(text: str) -> bool:
    """判断消息是否与花卉导购无关（纯寒暄/感谢）。用于 DONE 后判断是否开启新会话。"""
    t = text.strip().lower()
    if not t:
        return True
    if any(k in t for k in ('买', '送', '花', '束', '预算', '方案', 'diy', '自己', '店铺', '下单', '订单', '确认', '选', '要', '想要', '需要', '推荐', '生图', '效果', '图')):
        return False
    return any(w in t for w in _CHITCHAT_WORDS)
logger = logging.getLogger('agent')
_AFFIRMATIVE = ('好', '可以', '确认', '同意', '生成', '要', '行', '是', '看看')
# 否定词优先于肯定词判断（is_affirmative 先查 _NEGATIVE）。
# ⚠️ 第二组是「会命中 _AFFIRMATIVE 子串」的否定式，必须显式拦截——否则
# 「这个方案不行」(含「行」)、「不是这个」(含「是」)、「不好看 / 不想要」(含「好 / 要」)
# 会被判成**肯定**，进而误确认方案 / 误触发生图，表现为「答非所问」。
_NEGATIVE = (
    '不用', '不要', '不需要', '不必', '算了', '跳过', '无需', '别', '放弃',
    '不行', '不是', '不好', '不同意', '不确认', '不想要', '不合适', '不喜欢',
    '不满意', '不考虑', '换一个', '换一批',
)


def _platform_source_ids() -> list[str]:
    """扫描进程环境变量，返回已配置的平台数据源 source_id（小写去重排序）。

    两类来源（与 backend/data_gateway 保持一致，部署方注入容器环境变量）：
    - ``PLATFORM_DB_<SOURCE_ID>_URL``：直连只读库（需 active mapping）；
    - ``PLATFORM_API_<SOURCE_ID>_URL``：平台 REST 只读源（免 mapping，见 http_source.py）。

    未配置任何平台数据源时返回空列表，供 system prompt 如实告知 LLM。
    注：``PLATFORM_API_KEYS``（接入方鉴权）不以 ``_URL`` 结尾，天然不会被误判成数据源。
    """
    ids: set[str] = set()
    for key in os.environ:
        if not key.endswith('_URL'):
            continue
        for prefix in ('PLATFORM_DB_', 'PLATFORM_API_'):
            if key.startswith(prefix):
                mid = key[len(prefix):-len('_URL')]
                if mid:
                    ids.add(mid.lower())
                break
    return sorted(ids)


# 用户此轮是否在问「平台事实类」信息（店铺 / 商品）——命中则在**本轮** system prompt 追加
# 「必须先查证再回答」的指令。
# 背景（线上实测 2026-09-15）：问「有哪些店铺可以送花？」时模型**一轮零工具调用**直接作答，
# 编造出 3 家平台上根本不存在的店铺名，还配上了评分 / 营业时间 / 配送费 / 电话（全假）。
# 「平台信息只能来自查询结果」是硬约束，base.md 的场景描述约束力不够，故按轮注入强调。
_PLATFORM_SHOP_WORDS = (
    '店铺', '花店', '店家', '哪家', '哪几家', '几家店', '营业', '开门', '打烊', '几点关',
    '配送费', '起送', '送到', '配送', '包邮', '地址', '电话', '评分',
)
_PLATFORM_PLAN_WORDS = (
    '推荐', '有什么花', '在售', '多少钱', '价格', '库存', '有货', '哪款', '哪束',
    '方案', '花束', '商品', '款式', '有没有卖',
)


def _platform_fact_hint(message: str) -> str:
    """本轮是否在问平台事实类信息；返回需要的实体 ``'shop'`` / ``'plan'`` / ``''``。

    只做**方向性**判断（命中即注入「先查证」指令），不决定是否作答：
    误判的代价只是多一条约束，漏判的代价是编造店铺/价格，所以宁可偏宽。

    Args:
        message: 用户本轮原话。

    Returns:
        ``'shop'``（问店铺类）｜``'plan'``（问商品/价格类）｜``''``（未命中）。
    """
    text = message or ''
    if not text:
        return ''
    if any(w in text for w in _PLATFORM_SHOP_WORDS):
        return 'shop'
    if any(w in text for w in _PLATFORM_PLAN_WORDS):
        return 'plan'
    return ''


def _platform_queried(tool_log: list[Any], entity: str) -> bool:
    """本轮是否成功查过该实体的平台数据（用于「未查证不许作答」的判定）。

    Args:
        tool_log: 本轮工具调用记录（ToolCallRecord）。
        entity: ``plan`` / ``shop``。

    Returns:
        存在成功且 entity 匹配的 platform_db_query_entity 调用时为 True。
    """
    for tc in tool_log or []:
        if getattr(tc, 'name', '') != 'platform_db_query_entity':
            continue
        if getattr(tc, 'status', '') != 'ok':
            continue
        if str((getattr(tc, 'arguments', None) or {}).get('entity') or '') == entity:
            return True
    return False


def _platform_nudge_text(entity: str) -> str:
    """「未查证即作答」时注入的一次性纠正指令（只进本轮上下文，不落库）。"""
    sources = '、'.join(_platform_source_ids()) or '已配置数据源'
    what = ('店铺信息（店名 / 营业时间 / 配送费 / 起送价 / 地址 / 电话 / 评分）' if entity == 'shop'
            else '在售商品信息（商品名 / 价格 / 花材构成 / 库存）')
    return (
        '[系统校验未通过] 你刚才**没有查询平台数据**就直接作答了，而本轮用户问的是平台' + what + '。\n'
        f'请先调用 platform_db_query_entity(source_id="{sources}", entity="{entity}")'
        '（可用 keyword 缩小范围），拿到真实返回后再用 respond_to_user 回答。\n'
        '⚠️ 严禁凭印象写出具体店名 / 商品名 / 价格 / 营业时间 / 配送费等事实——'
        '编造这类信息是本系统最严重的错误（用户会照着去下单）。确实查不到时，就如实说明「平台上暂未查到」。'
    )


_CARD_TOOLS = ('generate_diy_plan', 'revise_diy_plan', 'show_plan_card')
# 终结工具族：调用其中任一 = 模型在本轮**选定了 UI 输出形态**并结束本轮。
# 三者返回结构同构（reply / ui / data / stage / intent / 结构化信号），下游统一按
# respond_args 消费，不必按工具名分支——新增「非文字输出」工具时只需登记到这里。
_TERMINAL_TOOLS = ('respond_to_user', 'show_plan_card', 'show_options')
# 延迟工具：**读**同轮其它工具写入的会话状态（生图取「最近一次 DIY 方案」），
# 所以必须等其余工具执行完再串行跑，不能并进并发批次。
_DEFERRED_TOOLS = ('generate_effect_image',)
# 「方案类追问」——命中这些词的问句应按知识问答作答，不该被强制拉回出卡
_PLAN_QUESTION_WORDS = (
    '为什么', '为何', '怎么养', '如何养', '养护', '寓意', '花语', '有毒', '能吃',
    '区别', '什么是', '什么意思', '能放多久', '几天', '怎么保存', '真的吗',
)


def _card_produced(tool_log: list[Any]) -> bool:
    """本轮是否产出了「用户可看的方案/商品/店铺」证据。

    用于区分「真的查了/真的生成了方案」与「只是把内容写进 reply 里」。
    """
    for tc in tool_log or []:
        if getattr(tc, 'status', '') != 'ok':
            continue
        name = getattr(tc, 'name', '')
        if name in _CARD_TOOLS:
            return True
        if name == 'platform_db_query_entity':
            ent = str((getattr(tc, 'arguments', None) or {}).get('entity') or '')
            if ent in ('plan', 'shop'):
                return True
    return False


def _expresses_flower_need(message: str) -> bool:
    """用户这句话里是否表达了花艺需求（收花人/场合/预算/颜色/风格/单一花材/支数任一）。

    直接复用需求抽取器——它本来就是「用户想要一束花」的判定器，比另建关键词表可靠。
    ⚠️ 不做 try/except 兜底：这里静默吞异常会**悄悄关掉整条防编造护栏**
    （第一版就是因此失效——`extract_requirement` 是 run() 内的局部导入，模块级不存在）。
    抽取器本身是纯正则、主链路每轮都在用，不存在比主链路更脆弱的可能。
    """
    text = (message or '').strip()
    if not text:
        return False
    from agent.tools import extract_requirement
    req = extract_requirement(text)
    return bool(
        req.recipient or req.occasion or req.style or req.colors
        or req.budget_num or req.stem_count or req.single_flower
    )


# 「模型已经在口述方案」的信号：① 出现具体花材数量（真在追问需求时几乎不会报支数）；
# ② 「方案」措辞 + 具体价格。用于首轮豁免的收口，见 _looks_like_plan_prose。
_PROSE_QTY_PATTERNS = (
    re.compile(r'\d{1,3}\s*[朵支枝]'),                        # 22朵 / 33枝
    re.compile(r'[\u4e00-\u9fff]{2,6}\s*[×xX]\s*\d{1,3}'),    # 洋桔梗×5
)
_PROSE_PRICE_RE = re.compile(r'\d{2,4}\s*元')


def _looks_like_plan_prose(reply: str) -> bool:
    """回复里是否已经**口述出实质方案**（而不是在追问需求或寒暄）。

    为什么需要（2026-09-16 线上实测）：首轮的 `has_context` 是 False，护栏整体豁免。
    但模型经常在**首轮就零工具调用地口述整套方案**——「香槟色洋桔梗×5 + 紫色风信子×5…
    预算 318 元」，用户既拿不到卡片、也无法核验真伪（此前实测两轮都是这种走法）。
    「首次提问用文字追问是合理的」这个豁免只应对**老实追问**成立，不该放过口述方案。

    Args:
        reply: 模型本轮已经产出的回复文本。

    Returns:
        看起来已在口述方案时返回 True。
    """
    text = (reply or '').strip()
    if not text:
        return False
    if any(p.search(text) for p in _PROSE_QTY_PATTERNS):
        return True
    return '方案' in text and bool(_PROSE_PRICE_RE.search(text))


def _needs_card_nudge(message: str, tool_log: list[Any], has_context: bool, reply: str = '') -> bool:
    """是否该拦下「只有文字、没有方案卡」的回复。

    线上实测（2026-09-16，演示实例）：用户先说「送妈妈一束花，预算200」拿到方案卡，
    再补「生日，粉色系，你决定就好」——模型**零工具调用**、3.7 秒直接写了两款"方案"
    （其中「樱雾甜梦」在平台上根本不存在），用户拿不到卡片，也无法核验真伪。

    判定刻意保守（误判要多花一轮 LLM）：
      · 命中知识类问句（为什么/怎么养/花语…）不拦——那是真该用文字答；
      · 必须这句话确实表达了花艺需求才拦；
      · **首轮**（`has_context` 为假）再收一道：只有模型**已经在口述方案**时才拦
        （`_looks_like_plan_prose`）。老实追问「送给谁、预算多少」仍然放行——
        那是合理交互，拦了反而变成硬出卡。

    Args:
        message: 用户本轮原话。
        tool_log: 本轮工具调用记录。
        has_context: 本会话是否已有方案卡或已追问过一轮。
        reply: 模型本轮已产出的回复文本（用于首轮判断是否在口述方案）。

    Returns:
        需要注入「必须出卡」纠正时为 True。
    """
    if _card_produced(tool_log):
        return False
    text = message or ''
    if any(w in text for w in _PLAN_QUESTION_WORDS):
        return False
    # ① **模型已经在口述方案**（回复里有具体花材数量 / 方案+具体价格）→ 必须出卡。
    #    这条刻意不依赖用户话里的关键词：用户可能说得很含蓄
    #    （「她最近心情不太好，我想让她开心一下」——正则抽不出任何"花艺需求"字段），
    #    但模型显然已经进入出方案的状态，此时不给卡就是让用户拿不到可核验的东西。
    #    （2026-09-16 实测：正是这种句子被漏掉，模型口述了整套方案却没出卡。）
    if _looks_like_plan_prose(reply):
        return True
    # ② 其余情况：用户这句话确实表达了花艺需求，且会话已有上下文 → 也拦。
    if not _expresses_flower_need(text):
        return False
    return has_context


def _card_nudge_text() -> str:
    """「只有文字没出卡」时注入的一次性纠正指令（只进本轮上下文，不落库）。"""
    return (
        '[系统校验未通过] 你刚才**只用文字描述了方案，并没有真的生成方案卡**。\n'
        '本会话此前已经有过方案（或已追问过一轮），用户这句话表达的是花艺需求，'
        '必须给出可点击的方案卡，不能只用文字描述：\n'
        '· 要定制搭配 → 调用 generate_diy_plan；要在此基础上调整 → 调用 revise_diy_plan；\n'
        '· 要平台在售的现成款 → 调用 platform_db_query_entity(source_id=..., entity="plan")；\n'
        '· 拿到工具返回后，用 show_plan_card 输出卡片。\n'
        '⚠️ reply 里的方案名 / 花材组合 / 支数 / 价格，都必须来自工具返回，'
        '严禁凭记忆或想象编造（写不存在的商品名或错价是最严重的错误）。'
    )


# ── 生图声明护栏（2026-09-16）────────────────────────────────────────────
# 线上实测（演示实例，连续两轮）：用户说「出个效果图」→ 模型**零工具调用**、2.6 秒回
# 「效果图任务已提交（AI 生成中，通常几十秒到几分钟）」；用户追问「图呢？」→ 又回
# 「这张是按「星河长明」的花材构成生成的参考图」。**全程没有任何生图任务**，
# 用户在等一张根本不存在的图。与「未查证即作答」同类：光靠 prompt 不够，必须确定性拦截。
# ⚠️ 判定必须是「**图词 + 完成态**的邻近组合」，**绝不能**用「提到图」+「提到生成」的宽松共现——
# 线上实测（2026-09-16）后者把普通的「我已经生成了方案」也算成「谎称已出图」，导致
# 用户问「目前这个数据库的信息包括哪些？」被白拦一轮、多烧 30 秒重答（日志留痕）。
_IMAGE_CLAIM_PATTERNS = (
    re.compile(r'(?:效果图|生图|图片|参考图|出图)[^。！？\n]{0,10}'
               r'(?:已提交|已生成|生成中|生成好了|已就绪|已出|正在生成|已完成)'),
    re.compile(r'(?:已提交|已生成|生成中|正在生成|已出图)[^。！？\n]{0,10}'
               r'(?:效果图|生图|图片|参考图|出图)'),
    # 「这张是…生成的参考图」= 在描述一张图的内容，等于暗示图已存在
    re.compile(r'(?:这张|这张图|上图|下图|配图)[^。！？\n]{0,20}(?:生成|参考图|效果图)'),
)
# 如实说明失败的字眼 → 不算「声称已出图」
_IMAGE_FAIL_WORDS = ('没有生成', '未生成', '无法生成', '生成失败', '没能生成', '还未生成', '尚未生成')

_IMAGE_UNVERIFIED_REPLY = (
    '抱歉，这条效果图**并没有真的生成成功**——我这边没有拿到生图任务，刚才的说法不准确。\n'
    '要我再试一次吗？直接说「用这个方案出一张效果图」就行。'
)


def _image_task_created(tool_log: list[Any]) -> bool:
    """本轮是否**真的**提交了生图任务（必须拿到 task_id，光调用不算）。

    Args:
        tool_log: 本轮工具调用记录。

    Returns:
        有成功且带 task_id 的 generate_effect_image 调用时为 True。
    """
    return bool(_extract_image_task(tool_log).get('task_id'))


def _extract_image_task(tool_log: list[Any]) -> dict[str, Any]:
    """取本轮**成功提交**的生图任务信息（task_id / poll / result_url）。

    与 :func:`_image_task_created` 同源判据：光"调用了"不算，必须拿到 task_id。
    供 UI 决议复用 —— 方案卡与生图任务并存时，把任务信息挂到卡片 data 上，
    前端据此轮询并在出图后补图（见 `demo/index.html` 的 task_id 处理）。

    Args:
        tool_log: 本轮工具调用记录。

    Returns:
        任务信息字典（取最近一次成功提交）；没有真实任务时返回空字典。
    """
    for tc in reversed(tool_log or []):
        if getattr(tc, 'status', '') != 'ok' or getattr(tc, 'name', '') != 'generate_effect_image':
            continue
        try:
            result = json.loads(tc.result) if isinstance(tc.result, str) else tc.result or {}
        except (json.JSONDecodeError, TypeError):
            result = {}
        if isinstance(result, dict) and result.get('task_id'):
            out: dict[str, Any] = {'task_id': result['task_id']}
            if result.get('poll'):
                out['poll'] = result['poll']
            if result.get('result_url'):
                out['result_url'] = result['result_url']
            return out
    return {}


def _claims_image_done(reply: str) -> bool:
    """回复是否在**声称**效果图已提交/已生成（而不是如实说明失败）。

    与 :func:`_image_task_created` 配对使用：声称了却没有任务 = 编造。

    ⚠️ 只认「**图词 + 完成态**的邻近组合」（见 ``_IMAGE_CLAIM_PATTERNS``）——
    宽松共现会大面积误伤普通回复（「我已经生成了方案」也会被判成谎称出图）。

    Args:
        reply: 本轮回复文本。

    Returns:
        看起来在声称已出图时返回 True。
    """
    text = reply or ''
    if not text:
        return False
    if any(w in text for w in _IMAGE_FAIL_WORDS):
        return False  # 如实说明失败 → 放行
    return any(p.search(text) for p in _IMAGE_CLAIM_PATTERNS)


def _needs_image_nudge(tool_log: list[Any], reply: str) -> bool:
    """是否该拦下「谎称效果图已生成/已提交」的回复。

    Returns:
        没有生图任务却在声称已出图时为 True。
    """
    return not _image_task_created(tool_log) and _claims_image_done(reply)


def _image_nudge_text() -> str:
    """「没有生图任务却声称已生成」时注入的一次性纠正指令（只进本轮上下文，不落库）。"""
    return (
        '[系统校验未通过] 你刚才声称效果图「已提交 / 已生成 / 生成中」，但**本轮并没有任何真实的'
        '生图任务**——用户会去找一张根本不存在的图，这是最严重的错误。\n'
        '现在请二选一：\n'
        '· 用户确实想要效果图 → 调用 generate_effect_image(plan="latest_diy")，'
        '拿到返回的 task_id 之后再如实告知「已提交，生成中」；\n'
        '· 生图不可用（如未找到可生图的方案）→ **如实说明**原因，并给出替代建议。\n'
        '⚠️ 没有 task_id 就绝不能说任务或图的存在，也不要描述一张「已生成的图」长什么样。'
    )


def _finalize_image_claim(reply: str, tool_log: list[Any]) -> str:
    """兜底：谎称已出图但本轮无生图任务 → 整段换成如实说明。

    放在清理链最后。护栏已先拦一次让模型自己改；仍不改就必须确定性兜底——
    让用户去找一张不存在的图，比回复难看严重得多。

    Args:
        reply: 清理链处理后的回复。
        tool_log: 本轮工具调用记录。

    Returns:
        可能被替换为如实说明的回复。
    """
    if _image_task_created(tool_log) or not _claims_image_done(reply):
        return reply
    logger.warning('[agent] 回复谎称已生成效果图但本轮无生图任务，已替换为如实说明')
    return _IMAGE_UNVERIFIED_REPLY


# ── 内部独白泄漏护栏 ──────────────────────────────────────────────────────
# 线上实测（2026-09-17，演示实例）：寒暄类消息（「谢谢你」「我心情不太好」）会返回模型的
# **内部独白**而不是面向用户的话：
#   「用户说"谢谢你"，这是一个感谢/告别的话语。我需要判断意图：… 我应该用
#     respond_to_user 工具，纯文字回复」—— 结尾还带出工具参数残片 {"reply"
# ui 判定是 text（看起来"通过"），但用户会直接看到推理过程和工具名 —— 第一印象级缺陷。
# 10 条用例里复现 2 次：低频但真实，且光靠 prompt 拦不住（本项目反复验证过）。
#
# 判据刻意分强弱两档，避免误伤正常回复：
#   · 强信号（命中一条即判定）：工具名、工具参数的 JSON 残片 —— 它们本来就不该出现在回复里；
#   · 元叙述信号（需命中 ≥2 条）：「用户说…」「我需要判断意图」「我应该用…」这类
#     模型对自己说话的口吻 —— 正常花艺回复不会出现，单条可能是巧合，两条基本确定。
_REASONING_TOOL_NAMES = (
    'respond_to_user', 'show_plan_card', 'show_options', 'generate_diy_plan',
    'revise_diy_plan', 'generate_effect_image', 'retrieve_knowledge',
    'platform_db_query_entity', 'search_history', 'get_user_profile',
    'save_user_profile', 'save_memory', 'suggest_greetings', 'render_greeting_card',
)
# 工具参数的 JSON 残片：可能带前括号，也可能是被截断后剩下的键值对（实测末尾就是 {"reply 这种半截）。
_REASONING_PAYLOAD_LEAK = re.compile(
    r'[{[]\s*"(?:reply|ui|data|stage|intent)"|"(?:reply|ui|data|stage|intent)"\s*:'
)

_REASONING_META_MARKERS = (
    '用户说', '用户问', '用户想', '用户希望', '用户表示', '用户的意思是',
    '我需要判断', '判断意图', '意图：', '我应该用', '我应该调用', '应该调用',
    '不需要调用任何工具', '不属于任何意图', 'chitchat',
)

_REASONING_FALLBACK_TEXT = '抱歉，刚才没说清楚 😅 想聊花、想要方案或者问养护，随时跟我讲～'
_REASONING_FALLBACK_CARD = '方案已经配好了，具体细节都在下面的卡片里，想调整哪部分随时跟我说～'


def _looks_like_reasoning_leak(reply: str) -> bool:
    """回复是否把**模型的内部独白**当成了给用户的话。

    Args:
        reply: 本轮回复文本。

    Returns:
        判定为内部独白泄漏时为 True。
    """
    text = reply or ''
    if not text:
        return False
    if any(t in text for t in _REASONING_TOOL_NAMES):
        return True
    if _REASONING_PAYLOAD_LEAK.search(text):
        return True
    return sum(1 for m in _REASONING_META_MARKERS if m in text) >= 2


# 「过程说明」粗筛特征（**仅用于流式推送前**，比 _looks_like_reasoning_leak 更宽松）。
#
# 为什么需要单独一套：流式是**边收边推**，等发现是独白再回滚已经晚了 ——
# 实测模型在工具轮会先自言自语几十字再调工具，头部缓冲（40 字）根本不够。
# 所以在**推送前**先用这份更宽松的特征表粗筛：命中就本轮不再推。
#
# 误判代价很低（只是不推流式，最终 reply 照常通过清理链给出），
# 所以这里刻意"宁可少推，不可漏推"——与最终护栏 _looks_like_reasoning_leak
# 的保守策略（需 ≥2 条元叙述）正好相反。
_PROCESS_NARRATION_MARKERS = (
    '用户说', '用户问', '用户想', '用户希望', '用户表示',
    '我注意到', '我意识到', '我需要判断', '我需要先', '我需要确认',
    '我应该', '应该调用', '判断意图', '意图：', '让我先', '我先查',
    'chitchat', '不属于任何意图',
)


def _has_process_narration(text: str) -> bool:
    """文本是否像模型的「过程说明」（流式推送前的粗筛）。

    Args:
        text: 已累计的流式内容。

    Returns:
        命中任一过程说明特征时为 True。
    """
    return any(m in (text or '') for m in _PROCESS_NARRATION_MARKERS)


def _extract_partial_json_string(raw: str, key: str) -> str:
    """从**可能尚未闭合**的 JSON 文本里，尽力提取某个字符串字段的当前值。

    为什么需要（2026-09-18 审计 P0-1 的关键发现）：flora 的最终回复**不走 LLM 的
    `content`**，而是通过终结工具（`respond_to_user` / `show_plan_card`）的 **JSON 参数
    `reply`** 传回来的 —— 实测三轮 LLM 调用的 `content` 全是 0 字、`tool_calls` 全是 1 个。
    所以要做真流式，就必须边收参数边把 `reply` 的值解析出来。

    容错优先于精确：解析到哪算哪，调用方据此**增量推送**；最终仍以 `done` 事件里
    经清理链处理过的完整 `reply` 为准，所以这里允许"少解析"（不会解析出错误内容，
    最坏情况是推得慢一点）。

    Args:
        raw: 正在累积的 JSON 字符串（可能被截断在任意位置）。
        key: 要提取的字段名，如 ``reply``。

    Returns:
        已能确定的部分值；字段还没出现 / 不是字符串时返回空串。
    """
    marker = f'"{key}"'
    i = raw.find(marker)
    if i < 0:
        return ''
    j = raw.find(':', i + len(marker))
    if j < 0:
        return ''
    k = j + 1
    while k < len(raw) and raw[k] in ' \t\r\n':
        k += 1
    if k >= len(raw) or raw[k] != '"':
        return ''
    k += 1
    out: list[str] = []
    while k < len(raw):
        ch = raw[k]
        if ch == '\\':
            if k + 1 >= len(raw):
                break                      # 转义符还没收全，等下一个 chunk
            nxt = raw[k + 1]
            mapped = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\', '/': '/'}.get(nxt)
            out.append(nxt if mapped is None else mapped)
            k += 2
            continue
        if ch == '"':
            break                          # 字符串正常结束
        out.append(ch)
        k += 1
    return ''.join(out)


def _needs_reasoning_nudge(reply: str) -> bool:
    """是否该拦下「把内部独白当回复」的这一轮。

    Args:
        reply: 本轮回复文本。

    Returns:
        需要注入纠正时为 True。
    """
    return _looks_like_reasoning_leak(reply)


def _reasoning_nudge_text() -> str:
    """「回复里出现内部独白」时注入的一次性纠正指令（只进本轮上下文，不落库）。"""
    return (
        '[系统校验未通过] 你刚才发出去的内容**不是给用户看的回复**，而是你自己在分析该怎么做'
        '——里面出现了「用户说…」「我需要判断意图」「我应该调用某个工具」这类内部推理，'
        '甚至带出了工具名和参数格式。用户看到会以为你在自言自语，这是严重的体验缺陷。\n'
        '现在请重新给出**真正面向用户**的那句话：\n'
        '· 只写用户该看到的内容（回答 / 追问 / 说明），保持自然口语；\n'
        '· 绝不写分析过程、意图判断、工具名、参数或任何 JSON。'
    )


def _finalize_reasoning_leak(reply: str, ui: Any) -> str:
    """兜底：纠正后回复仍是内部独白 → 整段换成面向用户的通用回复。

    放在清理链末尾。护栏已先拦一次让模型自己改；仍不改就必须确定性兜底 ——
    让用户读一段"我该怎么回答"的独白，比回复平淡严重得多。

    Args:
        reply: 清理链处理后的回复。
        ui: 本轮产出类型（卡片场景给对应的兜底文案）。

    Returns:
        可能被替换的回复。
    """
    if not _looks_like_reasoning_leak(reply):
        return reply
    logger.warning('[agent] 回复仍是内部独白，已替换为面向用户的兜底文案')
    return _REASONING_FALLBACK_CARD if ui in _CARD_UIS else _REASONING_FALLBACK_TEXT


_IMAGE_DECLINE_WORDS = (
    '不要生成', '不用生成', '别生成', '不生成', '不要出图', '不用出图', '别出图',
    '取消生图', '不要效果图', '不用效果图', '别效果图', '不要预览图', '不用预览图',
)
_IMAGE_TOPIC_WORDS = ('效果图', '生图', '出图', '预览图', '生成图')
_IMAGE_DECLINE_HINT = ('不要', '不用', '别', '取消', '不需要', '免了', '算了')


def _decline_image(message: str) -> bool:
    """用户是否明确表示不想生成效果图。

    用于「生图补调」的意图豁免：用户已经说不要了，就绝不能再强行生成——
    否则会出现用户说什么都回同一句「正在为您生成效果图预览」的死循环。
    """
    text = (message or '').strip()
    if not text:
        return False
    if any(w in text for w in _IMAGE_DECLINE_WORDS):
        return True
    # 「不用了」「别了」这类省略语，需同时出现图相关词才算拒绝生图
    return any(w in text for w in _IMAGE_TOPIC_WORDS) and any(w in text for w in _IMAGE_DECLINE_HINT)


async def _find_pending_image_task(user_id: str, sid: str) -> dict | None:
    """查找本会话已存在但仍在生成的任务，用于去重（绝不重复烧生图 API）。

    返回该任务信息；若最近那个任务已完成 / 失败 / 不存在，则返回 None。
    """
    try:
        from backend.storage.tasks import get_image_task
        for msg in reversed(await mem_store.load_display_messages(sid)):
            data = msg.get('data') if isinstance(msg.get('data'), dict) else {}
            task_id = data.get('task_id')
            if not task_id:
                continue
            task = await get_image_task(str(task_id))
            if not task:
                return None
            if task.get('result_url'):
                return None  # 已出图，不算 pending
            if task.get('status') == 'processing':
                return task
            return None  # failed / 其它终态不再复用
    except Exception:
        logger.exception('[agent] 查找未完成的生图任务失败')
    return None


def _entity_query_ok(tool_log: list[ToolCallRecord], entity: str) -> bool:
    """本轮是否有成功的 platform_db_query_entity 调用且 arguments.entity 匹配。

    entity 取值：plan / shop / order / user（对应平台只读查询的标准业务实体）。
    """
    return any(
        tc.status == 'ok'
        and tc.name == 'platform_db_query_entity'
        and (tc.arguments or {}).get('entity') == entity
        for tc in tool_log
    )


def is_allowed(role: str, action: str) -> bool:
    """角色权限检查（兼容接口）。"""
    return True

def is_affirmative(text: str) -> bool:
    """判断用户消息是否为明确肯定意图（关键词兜底版；主判据见 _resolve_affirmative）。"""
    t = (text or '').strip()
    if not t:
        return False
    if any(k in t for k in _NEGATIVE):
        return False
    return any(k in t for k in _AFFIRMATIVE)


# ── L1「对话理解」：结构化信号消费 ──────────────────────────────────────────
# 背景：此前「用户是否确认 / 要不要生图 / 是否换一批」全靠单字关键词猜，已两次踩坑——
# 「这个方案不行」含「行」被判成肯定 → 误确认方案；用户拒绝生图后被新方案冲掉重新生图。
# 现在改由 respond_to_user 携带的结构化信号（confirmation / image / wants_alternative）
# 主导判断，关键词只在信号缺失或非法时兜底。原则：**理解归 LLM，护栏归规则**。
_CONFIRM_SIGNALS = ('confirm', 'reject', 'none')
_IMAGE_SIGNALS = ('want', 'decline', 'none')
# 「换一批」关键词兜底（模型漏填 wants_alternative 时仍能识别）
_ALTERNATIVE_WORDS = ('再', '换', '别的', '预算', '有没有', '其他', '看看')
# 方案确认的显式短语。注：历史版本用过裸「这个方案」做子串匹配，导致
# 「这个方案不行」也被判成确认 → 故此处不再收录裸短语，并配合否定词护栏。
_PLAN_CONFIRM_PHRASES = (
    '确认方案', '确认这个方案', '就这个方案', '这个方案可以', '方案可以',
    '就这个', '定这个', '就它', '要这个', '选这个',
)


def _signal_arg(respond_args: dict | None, key: str, allowed: tuple[str, ...]) -> str | None:
    """读取 respond_to_user 的结构化信号；缺失 / 非法 / 显式 'none' 一律返回 None。

    返回 None 的语义是「本轮没有可靠信号」→ 调用方回退关键词判定。
    绝不抛异常：模型偶发幻觉或旧客户端不带该字段时，行为必须与改造前一致。

    Args:
        respond_args: respond_to_user / show_plan_card 的入参（可能为 None 或非 dict）。
        key: 字段名，如 'confirmation' / 'image'。
        allowed: 合法枚举值（含 'none'）。

    Returns:
        归一化后的小写枚举值；不合法或为 'none' 时返回 None。
    """
    if not isinstance(respond_args, dict):
        return None
    raw = respond_args.get(key)
    if not isinstance(raw, str):
        return None
    val = raw.strip().lower()
    if val not in allowed or val == 'none':
        return None
    return val


def _resolve_affirmative(message: str, signal: str | None) -> bool:
    """本轮用户是否「肯定」。LLM 信号优先，关键词兜底，规则保留否决权。

    - 消息命中否定词 → 恒 False（规则一票否决，先于任何信号）
    - signal == 'confirm' → True；'reject' → False
    - signal 缺失（None）→ 回退 is_affirmative(message)，与 L1 改造前逐字一致

    Args:
        message: 用户本轮原始消息。
        signal: _signal_arg 解析出的 confirmation 信号（可为 None）。

    Returns:
        是否视为肯定。
    """
    text = message or ''
    if any(k in text for k in _NEGATIVE):
        return False
    if signal == 'confirm':
        return True
    if signal == 'reject':
        return False
    return is_affirmative(text)


def _img_mentioned(text: str) -> bool:
    """消息是否提到效果图/生图（仅用于「想要图」的关键词兜底）。"""
    t = text or ''
    return any(w in t for w in _IMAGE_TOPIC_WORDS) or '生成' in t


def _resolve_image(message: str, signal: str | None) -> tuple[bool, bool]:
    """解析用户对效果图的态度，返回 (want, decline)。

    保守策略（生图要花钱，且「拒了又硬塞」是最伤体验的失败形态）：
    - decline 取**并集**：模型说 decline，或关键词命中「不要效果图」 → 拒绝
    - want 取值保守：模型说 want，或（关键词提到图相关 且 肯定）→ 想要；判定 decline 后恒 False
    - signal 缺失 → 与改造前关键词判定完全等价（零回归）

    Args:
        message: 用户本轮原始消息。
        signal: _signal_arg 解析出的 image 信号（可为 None）。

    Returns:
        (want, decline) 二元组，二者不会同时为 True。
    """
    text = message or ''
    if signal == 'decline' or _decline_image(text):
        return False, True
    want = signal == 'want' or (_img_mentioned(text) and is_affirmative(text))
    return want, False


def _wants_alternative(respond_args: dict | None, message: str) -> bool:
    """用户是否想「换一批 / 再看别的」。

    取并集而非严格主从：关键词命中即 True，模型布尔信号可额外补充。
    理由：清理 plan_ 标志是幂等无副作用的操作，多清一次不会出错；
    若让模型的 False 覆盖关键词命中，反而可能漏清、导致旧方案顽固复用。

    Args:
        respond_args: respond_to_user 入参。
        message: 用户本轮原始消息。

    Returns:
        是否需要清理「已推方案」标志。
    """
    if any(w in (message or '') for w in _ALTERNATIVE_WORDS):
        return True
    return isinstance(respond_args, dict) and respond_args.get('wants_alternative') is True


# ── L3 澄清式追问：缺关键信息时不硬编方案 ──────────────────────────────────
# 目标：把「模型缺信息也硬出一条猜的方案卡」变成「先问清楚再设计」。
# 判据用模型自报的 missing（它读得到完整上下文），agent 只做白名单过滤 + 确定性拦截；
# 关键词仅用于「用户是否已授权自行决定」这一豁免判断。
_CLARIFY_PRIORITY = ('recipient', 'occasion', 'budget')
# 用户已把这几个槽位交给系统决定 → 不再打断（否则会变成没完没了的表单式追问）
_CLARIFY_EXEMPT = (
    '随便', '你决定', '你定', '你看着办', '看着办', '都行', '都可以', '不用问',
    '别问', '直接来', '直接出', '推荐就行', '你推荐', '帮我挑', '帮我选',
)
_SLOT_ASK = {
    'recipient': '送给谁（妈妈 / 女朋友 / 朋友 / 长辈 / 同事…）',
    'occasion': '什么场合（生日 / 纪念日 / 求婚 / 探病 / 开业 / 感谢…）',
    'budget': '预算大概多少（100 以内 / 200 左右 / 300-500 / 500 以上）',
}


def _has_any_requirement(req: Any) -> bool:
    """会话累积需求里是否至少有一项有效信号（与 tools._req_clear 同语义）。

    仅用于 L3 追问的「模型漏报兜底」：一项都没有，说明用户其实什么都没说清。
    """
    return bool(req and (req.recipient or req.occasion or req.budget_num is not None
                         or req.budget_anchor or req.scene or req.style or req.colors))


def _clarify_slots(
    message: str,
    respond_args: dict | None,
    ui: UIType,
    diy_produced: bool,
    asked_before: bool,
    session_req: Any = None,
) -> list[str]:
    """本轮是否该「先追问、别硬出方案」；返回要追问的槽位（空列表 = 不追问）。

    只在 **DIY 定制路径**（本轮真的产出了 DIY 方案）上生效——平台在售推荐是另一条路，
    用户看的是现成商品，追问反而多事，故用 ``diy_produced`` 区分。
    只问 recipient / occasion / budget 三项：style / colors 缺失时模型可以主动推荐，
    不必为它们打断用户。

    Args:
        message: 用户本轮原始消息。
        respond_args: respond_to_user / show_plan_card 入参（携带模型自报的 missing）。
        ui: 本轮推导出的 UI 类型（只有 plan_card 才需要拦）。
        diy_produced: 本轮是否有成功的 generate_diy_plan / revise_diy_plan。
        asked_before: 本会话是否已追问过一次（同一需求内不重复唠叨）。
        session_req: 本会话累积的结构化需求；用于「模型漏报 missing」时的兜底判据。

    Returns:
        需要追问的槽位列表（按 _CLARIFY_PRIORITY 顺序，最多 2 个）。
    """
    if ui != UIType.PLAN_CARD or not diy_produced or asked_before:
        return []
    if any(w in (message or '') for w in _CLARIFY_EXEMPT):
        return []
    raw = respond_args.get('missing') if isinstance(respond_args, dict) else None
    reported = {str(x).strip().lower() for x in raw} if isinstance(raw, list) else set()
    slots = [s for s in _CLARIFY_PRIORITY if s in reported][:2]
    if not slots and session_req is not None and not _has_any_requirement(session_req):
        # 模型漏报兜底：会话累积需求里**一项有效信号都没有**（用户确实什么都没说清）→
        # 只问最关键的两项。仅在「完全无信号」时触发，避免对已说清需求的用户唠叨。
        slots = ['recipient', 'occasion']
    return slots


def _append_clarify(reply: str, slots: list[str]) -> str:
    """把追问句**追加**到模型回复之后（不替换——系统动作只追加的原则见 H-1 教训）。

    Args:
        reply: 模型本轮回复（可能为空）。
        slots: 待追问的槽位列表。

    Returns:
        追加追问后的回复；无有效槽位时原样返回。
    """
    asks = '；'.join(_SLOT_ASK[s] for s in slots if s in _SLOT_ASK)
    if not asks:
        return reply
    base = (reply or '').strip()
    # 模型**自己**已经把追问问出来了 → 不再追加模板句。
    # 为什么（2026-09-16「要像真人对话，不要机械走流程」）：固定模板
    # 「在给你定方案之前，想先确认一下：…？」会一轮一轮一字不差地重复，是我们最明显的
    # 机械感来源；模型自己组织问法远好于模板。这里退化为「它忘了问」时的兜底。
    if base and ('？' in base or '?' in base):
        return base
    tail = f'在给你定方案之前，想先确认一下：{asks}？'
    return f'{base}\n{tail}' if base else tail

class ReActAgent:
    """基于 ReAct + 状态机的导购智能体。"""

    async def arun(self, user_id: str, message: str, session_id: str | None=None, location: dict[str, float] | None=None, shop_id: str | None=None, entry: str | None=None, product_id: str | None=None, product_title: str | None=None) -> ChatResponse:
        """异步入口：用线程池跑同步主循环。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_AGENT_EXECUTOR, lambda: asyncio.run(self.run(user_id, message, session_id, location, shop_id=shop_id, entry=entry, product_id=product_id, product_title=product_title)))

    async def arun_stream(self, user_id: str, message: str, session_id: str | None=None, location: dict[str, float] | None=None, shop_id: str | None=None, entry: str | None=None, product_id: str | None=None, product_title: str | None=None):
        """流式异步入口：yield SSE 事件字典，供 /chat/stream 消费。

        事件类型：
        - {"event": "tool_call", "name": "...", "status": "ok/error"}
        - {"event": "text_delta", "content": "..."}   — 最终回复**逐字**推送（打字机效果）
        - {"event": "text_rollback"}                  — 撤回上面已推的文字（那段不是给用户看的）
        - {"event": "text", "content": "..."}         — 兼容旧契约的整段文字
        - {"event": "card", "ui": "...", "data": {...}}  — 结构化卡片
        - {"event": "done", "session_id", "reply", "ui", "data", "ai_generated", "content_disclosure"}
          — ⚠️ **以这里的 `reply` 为准**：流式推的是模型原始输出，这里是清理链处理后的最终版
          （危险标签已转义、独白已替换、卡片要点可能已追加）。
        - {"event": "error", "message": "..."}
        """
        try:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[dict | None] = asyncio.Queue()

            def _on_event(evt: dict) -> None:
                """run() 线程中调用，线程安全地把事件推入 async Queue。"""
                loop.call_soon_threadsafe(queue.put_nowait, evt)

            async def _run():
                # 关键（P0）：无论成功 / 异常 / 超时，都必须补一个 None 结束哨兵，
                # 否则消费端 `await queue.get()` 会永久阻塞 —— SSE 挂死、用户转圈不停。
                try:
                    result = await asyncio.wait_for(
                        loop.run_in_executor(_AGENT_EXECUTOR, lambda: asyncio.run(self.run(user_id, message, session_id, location, on_event=_on_event, shop_id=shop_id, entry=entry, product_id=product_id, product_title=product_title))),
                        timeout=settings.request_timeout,
                    )
                    # done 事件带**经清理链处理过**的完整结果：流式期间推的是模型原始
                    # content，清理链可能做了危险标签转义 / 独白替换 / 追加卡片要点，
                    # 接入方应在 done 时用它覆盖已推的文字（2026-09-18 审计 P0-1）。
                    # ⚠️ 全部用 getattr 兜底：result 若是替身对象/异常路径可能缺字段，
                    # 而这里一旦抛异常就会退化成 error 事件，前端连 done 都收不到。
                    _ui = getattr(result, 'ui', None)
                    await queue.put({
                        'event': 'done',
                        'session_id': getattr(result, 'session_id', '') or '',
                        'reply': getattr(result, 'reply', '') or '',
                        'ui': getattr(_ui, 'value', 'text') if _ui is not None else 'text',
                        'data': getattr(result, 'data', None) or {},
                    })
                except asyncio.TimeoutError:
                    logger.warning('[agent] 流式对话超时（%.0fs）', settings.request_timeout)
                    await queue.put({'event': 'error', 'message': '处理超时，请简化问题后重试'})
                except Exception:
                    logger.exception('[agent] 流式对话执行失败')
                    await queue.put({'event': 'error', 'message': '处理过程中出现错误，请稍后重试'})
                finally:
                    await queue.put(None)
            task = loop.create_task(_run())
            try:
                while True:
                    evt = await queue.get()
                    if evt is None:
                        break
                    yield evt
            except asyncio.CancelledError:
                task.cancel()
                raise
            finally:
                if not task.done():
                    task.cancel()
        except Exception as exc:
            logger.exception('[agent] arun_stream 异常')
            yield {'event': 'error', 'message': f'智能体执行失败: {type(exc).__name__}'}

    async def run(self, user_id: str, message: str, session_id: str | None, location: dict[str, float] | None, on_event: Callable[[dict], None] | None=None, shop_id: str | None=None, entry: str | None=None, product_id: str | None=None, product_title: str | None=None) -> ChatResponse:
        t0 = time.perf_counter()
        # 本轮硬性时间预算（P0 防线程裸跑）：比调用方 wait_for(REQUEST_TIMEOUT) 略早收口，留收尾余量。
        _deadline = t0 + max(10.0, settings.request_timeout - _DEADLINE_MARGIN)
        # 占位 shop_id（default/none/…）一律视为未锁店：前端从首页等非店铺入口进入时会
        # 传 shop_id='default'，若不规范化会被当成真实店铺锁死会话 → 查什么都查不到。
        shop_id = normalize_shop_id(shop_id)
        product_id = normalize_product_id(product_id)
        product_title = normalize_product_title(product_title)
        entry = normalize_entry(entry, shop_id, product_id)
        sid = await mem_store.get_or_create_session(user_id, session_id, shop_id=shop_id, entry=entry, product_id=product_id, product_title=product_title)
        # 店铺/入口上下文绑定在会话上，以会话存储的为准（创建时写入，整个会话不变）；
        # 存量会话里可能已写入 'default' 等占位值，读取时同样规范化掉。
        sess_ctx = await mem_store.get_session_context(sid)
        shop_id = normalize_shop_id(sess_ctx.get('shop_id')) or shop_id
        product_id = normalize_product_id(sess_ctx.get('product_id')) or product_id
        product_title = normalize_product_title(sess_ctx.get('product_title')) or product_title
        entry = normalize_entry(sess_ctx.get('entry'), shop_id, product_id)
        stage = SessionStage(await mem_store.get_stage(sid))
        if stage == SessionStage.DONE and (not _is_chitchat(message)):
            sid = await mem_store.create_conversation(user_id, title=message[:20], shop_id=shop_id, entry=entry, product_id=product_id, product_title=product_title)
            stage = SessionStage.ANALYZE
        # ── 会话级需求记忆（跨轮槽位累积）──
        # 历史缺陷：需求每轮只从**当前这条消息**抽取。用户分多轮补充（「送妈妈」→「生日」→
        # 「预算200」）时，前面说过的信息进不了结构化需求——LLM 靠对话历史还记得，但规则引擎
        # 拿不到，于是「LLM 失败回退 baseline」时方案明显缺信息。这里把本轮抽取合并进会话累积
        # 需求，并注入工具上下文（DIY 设计、澄清追问判据都用它）。
        req_acc: Any = None
        try:
            from domain.requirements import accumulate
            from agent.tools import extract_requirement
            req_acc = accumulate(await mem_store.get_requirement(sid), extract_requirement(message))
            if location and not req_acc.location:
                req_acc.location = location
            await mem_store.set_requirement(sid, req_acc)
        except Exception:
            logger.exception('[agent] 会话需求累积失败')
        stage = SessionStage(await mem_store.get_stage(sid))
        # ── 阶段粘滞治理（2026-09-16）──
        # IMAGE_GEN 表示「图正在生成」这个**事实**，不是「话题锁定」。图出完就该退出，
        # 否则每轮都会注入 stage_image_gen.md 的「图正在生成中」，模型被这个**假前提**带得
        # 只会聊图、答非所问。线上实测：用户问「目前这个数据库的信息包括哪些？」，
        # 阶段仍停在 image_gen → 模型答非所问地给了方案卡，还多烧一轮。
        if stage == SessionStage.IMAGE_GEN:
            try:
                _pending_img = await _find_pending_image_task(user_id, sid)
            except Exception:
                _pending_img = None
            if not _pending_img:
                stage = SessionStage.DIY_DESIGN
                logger.info('[agent] 无待处理的生图任务，阶段退出 image_gen')
        incoming = stage
        # 此处早于 LLM 调用，拿不到本轮结构化信号，故仍用关键词做「上一轮已进入生图阶段
        # 且用户肯定」的快速标记（保守方向：只多标一次 image_confirmed，不触发生图）。
        # 主判据在 _post_process 段 2（_resolve_image：模型信号优先 + 关键词兜底）。
        if stage == SessionStage.IMAGE_GEN and is_affirmative(message):
            await mem_store.set_session_flag(user_id, sid, 'image_confirmed', '1')
        long_term = await mem_store.get_long_term(user_id)
        history = await mem_store.load_history(sid, settings.history_limit)
        # 历史里的方案卡数据（data）不会作为可读内容发给模型 → 抽成摘要注入 prompt，
        # 让模型回答方案细节时有权威依据（避免凭记忆复述造成花材/配色失真）。
        current_plan = _latest_plan_summary(history)
        # 本轮问的是平台事实类信息（店铺/商品）→ 注入「必须先查证」指令，防编造（见 _platform_fact_hint）
        platform_facts = _platform_fact_hint(message)
        platform_sources = _platform_source_ids()
        _platform_nudge_left = 1  # 「未查证即作答」只纠正一次，避免与模型拉锯
        # 「只有文字没出卡」的判定上下文。三条任一成立即算「该出卡了」：
        #   ① 历史里已有方案卡；② 本会话已追问过一轮；③ **用户已经说过话（非首轮）**。
        # ③ 是必需的兜底：实测模型在纯文字追问时**并不填 `missing`**，也不产出卡片，
        #    于是 ② 永远落不上标记（2026-09-16 二次复现），只能按轮次兜住。
        _card_nudge_left = 1
        _image_nudge_left = 1  # 「谎称已生成效果图」也只纠正一次
        _reasoning_nudge_left = 1  # 「把内部独白当回复」同样只纠正一次
        _prior_user_turns = sum(1 for m in history if str(m.get('role')) == 'user')
        _has_plan_context = (
            bool(current_plan)
            or await mem_store.get_session_flag(user_id, sid, 'clarify_asked') == '1'
            or _prior_user_turns >= 1
        )
        system = self._build_system(stage, long_term, shop_id=shop_id, entry=entry, product_id=product_id, product_title=product_title, current_plan=current_plan, platform_facts=platform_facts)
        # 只把 role/content 发给 LLM：ui/data 是本系统内部的卡片结构，既不是模型该读的
        # 内容，也不该出现在请求体里（此前原样透传，属无意义载荷）。
        messages: list[dict[str, Any]] = [{'role': 'system', 'content': system}]
        messages += [{'role': str(m.get('role') or 'user'), 'content': str(m.get('content') or '')} for m in history]
        messages.append({'role': 'user', 'content': message})
        tool_log: list[ToolCallRecord] = []
        respond_args: dict[str, Any] | None = None
        final_reply = ''
        _any_pushed = False     # 本轮是否已通过真流式逐字推过文字（决定末尾要不要整段兜底）
        new_msgs: list[dict[str, Any]] = [{'role': 'user', 'content': message}]
        for turn in range(1, settings.max_iterations + 1):
            _remaining = _deadline - time.perf_counter()
            if _remaining <= _MIN_ITER_BUDGET:
                logger.warning('[agent] 时间预算耗尽（剩余 %.1fs），第 %d 轮主动收尾', _remaining, turn)
                final_reply = final_reply or '这个问题有点绕，我先给你一个初步建议；要更细致的可以咱们分步慢慢聊～'
                break
            logger.info('[agent] ReAct 第 %d/%d 轮 阶段=%s', turn, settings.max_iterations, stage.value)
            try:
                # 按本轮剩余预算收紧单次 LLM 超时，避免「8 轮 × 3 重试 × 120s」把整轮拖长。
                # 有 on_event（SSE 场景）时走**流式**：最终回复逐字推给用户，不再干等
                # （2026-09-18 外部安全审计 P0-1）；无 on_event（非流式 /chat）保持同步调用。
                if on_event is not None:
                    msg = self._stream_llm(messages, on_event, _remaining)
                    _any_pushed = _any_pushed or bool(getattr(msg, '_pushed', False))
                else:
                    resp = call_llm(messages, tools=to_openai_tools(), timeout=min(settings.llm_timeout, _remaining))
                    msg = resp.choices[0].message
            except Exception as exc:
                # 安全：原始异常只落服务端日志（含完整 traceback），绝不回显给用户，避免泄露
                # endpoint / 模型 ID / 密钥前缀 / 内部堆栈等敏感信息（K-1 修复）。
                logger.exception('[agent] LLM 调用失败')
                final_reply = '抱歉，我这边服务暂时开小差了，请稍后再试一次～'
                break
            # 注意：`msg` 已在上面两个分支里分别赋值（流式 / 非流式），此处**不能**再写
            # `msg = resp.choices[0].message` —— 流式分支里没有 `resp`，会 UnboundLocalError。
            tool_calls = self._parse_tool_calls(msg)
            if tool_calls:
                assistant_msg = {'role': 'assistant', 'content': getattr(msg, 'content', '') or '', 'tool_calls': [{'id': tc['id'], 'type': 'function', 'function': {'name': tc['name'], 'arguments': json.dumps(tc['arguments'], ensure_ascii=False)}} for tc in tool_calls]}
                messages.append(assistant_msg)
                new_msgs.append({**assistant_msg, 'content': ''})
                # ── 工具执行：终结工具只取参数；其余并发跑（互不依赖）──
                # 为什么要并发：模型一次发多个调用时，旧实现逐个 await，用户要为每一次
                # 网络往返分别等待。并发后总耗时 ≈ 最慢的那个，而不是累加。
                # ⚠️ 结果必须**按模型给出的原始顺序**回填（messages / tool_log / 事件）：
                #    · OpenAI 协议要求 tool 消息与 assistant.tool_calls 一一对应；
                #    · _derive_ui 依赖 reversed(tool_log) 取「最后一次产卡工具」，顺序错会选错卡。
                results: dict[int, tuple[str, str]] = {}
                exec_idx: list[int] = []
                defer_idx: list[int] = []
                for i, tc in enumerate(tool_calls):
                    name = tc.get('name') or ''
                    if name in _TERMINAL_TOOLS:
                        # 终结工具不执行：它的参数本身就是本轮结论（reply / ui / data）。
                        respond_args = tc['arguments']
                        results[i] = (json.dumps(respond_args, ensure_ascii=False), 'terminal')
                        continue
                    (defer_idx if name in _DEFERRED_TOOLS else exec_idx).append(i)

                # 注入会话累积需求：工具侧可用它补全跨轮信息（如 DIY 设计只传了「11朵粉玫瑰」，
                # 前几轮说过的「送妈妈、预算200」仍生效）。
                tool_ctx = {'user_id': user_id, 'session_id': sid, 'location': location,
                            'shop_id': shop_id, 'entry': entry, 'product_id': product_id,
                            'product_title': product_title, 'requirement': req_acc}
                if len(exec_idx) > 1:
                    gathered = await asyncio.gather(
                        *(execute_tool(tool_calls[i]['name'], tool_calls[i]['arguments'], tool_ctx)
                          for i in exec_idx),
                        return_exceptions=True,
                    )
                    for i, item in zip(exec_idx, gathered):
                        if isinstance(item, BaseException):
                            # 单个工具炸掉不影响同批其它工具（execute_tool 本身也兜了异常，
                            # 这里防的是它之外的问题——如取消/超时）。
                            logger.exception('[agent] 并发工具执行异常: %s',
                                             tool_calls[i]['name'], exc_info=item)
                            results[i] = (f'工具执行失败: {item}', 'error')
                        else:
                            results[i] = item
                    logger.info('[agent] 同轮并发执行 %d 个工具: %s',
                                len(exec_idx), [tool_calls[i]['name'] for i in exec_idx])
                elif exec_idx:
                    i = exec_idx[0]
                    results[i] = await execute_tool(tool_calls[i]['name'],
                                                    tool_calls[i]['arguments'], tool_ctx)

                for i in defer_idx:
                    results[i] = await execute_tool(tool_calls[i]['name'],
                                                    tool_calls[i]['arguments'], tool_ctx)

                for i in sorted(results):
                    tc = tool_calls[i]
                    result, status = results[i]
                    if status == 'terminal':
                        messages.append({'role': 'tool', 'content': result, 'tool_call_id': tc.get('id', '')})
                        new_msgs.append({'role': 'tool', 'content': result, 'tool_call_id': tc.get('id', '')})
                        continue
                    record = ToolCallRecord(name=tc['name'], arguments=tc['arguments'], result=result, status=status)
                    tool_log.append(record)
                    if on_event:
                        on_event({'event': 'tool_call', 'name': tc['name'], 'status': status})
                    messages.append({'role': 'tool', 'content': result, 'tool_call_id': tc.get('id', '')})
                    new_msgs.append({'role': 'tool', 'content': result, 'tool_call_id': tc.get('id', '')})
                if respond_args is not None:
                    # 平台事实类提问却整轮没查过平台 → 不让模型凭想象作答。
                    # 线上实测（2026-09-15）：问「有哪些店铺可以送花？」编造了 3 家不存在的店铺；
                    # 修店铺分支后，问「推荐送妈妈的花束」又编造了 3 个不存在的商品名与价格。
                    # 这里做**确定性拦截**：注入一次性纠正（只进本轮 LLM 上下文，不落库、不入历史），
                    # 要求先查再答；只拦一次，避免与模型拉锯。
                    if (_platform_nudge_left and platform_facts and platform_sources
                            and not _platform_queried(tool_log, platform_facts)):
                        _platform_nudge_left -= 1
                        messages.append({'role': 'user', 'content': _platform_nudge_text(platform_facts)})
                        respond_args = None
                        logger.warning(
                            '[agent] 平台事实类提问未查询平台（entity=%s），已注入纠正并要求重答', platform_facts,
                        )
                        continue
                    # 「只有文字、没有方案卡」→ 同样拦一次。
                    # 线上实测（2026-09-16）：已有方案的会话里，用户补一句「生日，粉色系，你决定就好」，
                    # 模型零工具调用直接写了两个方案（其中一个商品名平台上根本不存在）。
                    if _card_nudge_left and _needs_card_nudge(
                            message, tool_log, _has_plan_context,
                            str((respond_args or {}).get('reply') or final_reply or '')):
                        _card_nudge_left -= 1
                        messages.append({'role': 'user', 'content': _card_nudge_text()})
                        respond_args = None
                        logger.warning('[agent] 本轮只产出文字、未生成方案卡，已注入纠正并要求重答')
                        continue
                    # 「谎称已生成效果图」→ 同样拦一次（没有 task_id 就不能说图的存在）。
                    if _image_nudge_left and _needs_image_nudge(
                            tool_log, str((respond_args or {}).get('reply') or final_reply or '')):
                        _image_nudge_left -= 1
                        messages.append({'role': 'user', 'content': _image_nudge_text()})
                        respond_args = None
                        logger.warning('[agent] 未提交生图任务却声称已出图，已注入纠正并要求重答')
                        continue
                    # 「把内部独白当回复」→ 同样拦一次。实测这条路径（零工具调用直接回文字）
                    # 正是泄漏高发处：模型把"我该怎么答"的推理当成了回复。
                    if _reasoning_nudge_left and _needs_reasoning_nudge(
                            str((respond_args or {}).get('reply') or final_reply or '')):
                        _reasoning_nudge_left -= 1
                        messages.append({'role': 'user', 'content': _reasoning_nudge_text()})
                        respond_args = None
                        logger.warning('[agent] 回复里出现内部独白，已注入纠正并要求重答')
                        continue
                    break
                continue
            else:
                final_reply = getattr(msg, 'content', '') or ''
                # ⚠️ 模型**一个工具都没调**、直接回一段文字，同样可能是「该出卡却只写文字」。
                # 两条护栏在这里也必须生效——此前它们只守在 `respond_to_user` 分支里，
                # 线上实测（2026-09-16）模型正是走了这条路径（零工具调用、4.5 秒回文字），
                # 导致护栏形同虚设、还谎称「明细在卡片里」。
                if (_platform_nudge_left and platform_facts and platform_sources
                        and not _platform_queried(tool_log, platform_facts)):
                    _platform_nudge_left -= 1
                    messages.append({'role': 'assistant', 'content': final_reply})
                    messages.append({'role': 'user', 'content': _platform_nudge_text(platform_facts)})
                    final_reply = ''
                    logger.warning('[agent] 未调工具即答平台事实（entity=%s），已注入纠正并要求重答', platform_facts)
                    continue
                if _card_nudge_left and _needs_card_nudge(message, tool_log, _has_plan_context, final_reply):
                    _card_nudge_left -= 1
                    messages.append({'role': 'assistant', 'content': final_reply})
                    messages.append({'role': 'user', 'content': _card_nudge_text()})
                    final_reply = ''
                    logger.warning('[agent] 未调工具且未出卡，已注入纠正并要求重答')
                    continue
                # 「谎称已生成效果图」在这条路径同样成立（实测正是这条路：零工具调用直接回文字）。
                if _image_nudge_left and _needs_image_nudge(tool_log, final_reply):
                    _image_nudge_left -= 1
                    messages.append({'role': 'assistant', 'content': final_reply})
                    messages.append({'role': 'user', 'content': _image_nudge_text()})
                    final_reply = ''
                    logger.warning('[agent] 未提交生图任务却声称已出图，已注入纠正并要求重答')
                    continue
                # 「把内部独白当回复」在这条路径尤其常见（寒暄类消息最容易触发）。
                if _reasoning_nudge_left and _needs_reasoning_nudge(final_reply):
                    _reasoning_nudge_left -= 1
                    messages.append({'role': 'assistant', 'content': final_reply})
                    messages.append({'role': 'user', 'content': _reasoning_nudge_text()})
                    final_reply = ''
                    logger.warning('[agent] 回复里出现内部独白，已注入纠正并要求重答')
                    continue
                messages.append({'role': 'assistant', 'content': final_reply})
                break
        else:
            if any(tc.status == 'ok' for tc in tool_log):
                final_reply = final_reply or '我已经为你整理好相关结果啦，请查看下方卡片～'
            else:
                final_reply = final_reply or '抱歉，我思考得太久啦，请简化需求或分步骤再问我～'
        # ── 兜底出卡：纠正过一次后模型仍不肯出卡 → 用**规则引擎**确定性地补一张 ──
        # 线上实测（2026-09-16）：模型连续两轮都只写文字，还谎称「明细在卡片里」。
        # 与其让客户看到一张不存在的卡，不如给一张朴素但真实的方案卡（零 LLM 成本）。
        if (_card_nudge_left == 0 and not _card_produced(tool_log)
                and _needs_card_nudge(message, tool_log, _has_plan_context, final_reply)):
            try:
                from agent.tools import _build_plan, extract_requirement
                _dims = req_acc.to_legacy_dict() if req_acc is not None else extract_requirement(message).to_legacy_dict()
                _fb = _build_plan(_dims or {})
                respond_args = {
                    'reply': '按你的要求配好了这束，细节都在卡片里，想调整随时说～',
                    'ui': UIType.PLAN_CARD.value,
                    'data': {'plans': [_fb]},
                    'stage': SessionStage.VIEW_PLAN.value,
                    'intent': 'design',
                }
                logger.warning('[agent] 模型未出卡，已用规则引擎兜底生成方案卡 plan_id=%s', _fb.get('plan_id'))
            except Exception:
                logger.exception('[agent] 兜底出卡失败（不影响主流程）')
        new_stage, ui, data, final_reply, llm_intent = await self._post_process(
            respond_args, tool_log, incoming, message, final_reply, user_id, sid, location, new_msgs,
            session_req=req_acc,
        )
        # 商品卡与**最终**回复对齐：必须在 final_reply 定型之后做——_post_process 内部
        # 调 _derive_ui 时 reply 还没被 respond_to_user 覆盖，在那里按文案过滤会失效
        # （线上实测：文案推 5 款、卡片给 8 款无关商品）。
        data = _align_card_data_with_reply(ui, data, final_reply)
        # 排版兜底：删掉 LLM 回复里独立的分隔线行（--- / *** / ——），改为靠 prompt
        # 规则让其用数字编号分段；此处只做删除不做改写，不碰卡片数据。
        final_reply = _strip_separator_lines(final_reply)
        # 隐私兜底：去掉回复里残留的工具名 / 内部字段 / 原始数据行，
        # 确保数据库查询结果与内部实现不出现在前端（prompt 约束 + 代码兜底双保险）。
        final_reply = _strip_internal_leak(final_reply)
        # XSS 兜底：把回复里的危险 HTML 标签转义掉。reply 是纯文本，但任何不转义的
        # 接入端都会把 <script> 原样渲染 —— 安全不能只靠下游自觉（2026-09-18 审计 P0）。
        final_reply = _neutralize_dangerous_tags(final_reply)
        # 要点兜底：带卡片但回复过短时，追加一句基于卡片数据的可读要点
        # （模型有卡片时倾向只说「看卡片」，prompt 约束不稳定，此处确定性补齐）。
        final_reply = _ensure_card_summary(final_reply, ui, data)
        # 非空兜底：脱敏可能把正文整段清空（模型把原始数据行当正文输出时），
        # 纯文本场景下此前的兜底覆盖不到 → 用户看到空气泡。此处必须放在清理链**最后**。
        final_reply = _ensure_non_empty_reply(final_reply, ui)
        # 体验版（不承接下单）兜底：回复里若提到交易，补一句边界说明。
        if not _shop_entity_enabled():
            final_reply = _demo_trade_note(final_reply)
        # 生图声明兜底：护栏纠正后仍谎称已出图 → 整段换成如实说明
        # （用户不该被引导去找一张不存在的图）。
        final_reply = _finalize_image_claim(final_reply, tool_log)
        # 内部独白兜底：纠正后仍是「我该怎么回答」的推理 → 整段换成面向用户的话
        # （用户读到模型的自我分析，比回复平淡严重得多）。
        final_reply = _finalize_reasoning_leak(final_reply, ui)
        # （原先此处重复计算过一个 _img_intent，从未被使用——生图意图判断已统一在
        #   _post_process 内经 _resolve_image 消费结构化信号，故删除。）
        new_msgs.append({'role': 'assistant', 'content': final_reply, 'ui': ui.value, 'data': data})
        await mem_store.save_messages(sid, new_msgs)
        await mem_store.update_stage(sid, new_stage.value)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info('[agent] 完成 阶段=%s ui=%s 耗时=%.0fms', new_stage.value, ui.value, elapsed)
        if on_event:
            # 真流式（`_stream_llm` 逐字推 `text_delta`）已经推过，就不再重复推；
            # 只有**一次都没推过**时才退回整段推一次 `text`（兼容非流式路径 / 全部被拦的情况）。
            # ⚠️ 原实现是「把整段按句切碎 + sleep(0.03) 假装在流式」—— 那正是审计说的
            # 「90 秒只推 2 个整段」的伪流式，现已由真 token 流取代（2026-09-18 P0-1）。
            if not _any_pushed:
                on_event({'event': 'text', 'content': final_reply or ''})
            if ui and ui.value != 'text':
                on_event({'event': 'card', 'ui': ui.value, 'data': data})
        action_type = {
            UIType.PLAN_CARD: AgentActionType.SHOW_PLAN,
            UIType.SHOP_CARD: AgentActionType.SHOW_SHOP,
            UIType.ORDER_CARD: AgentActionType.CREATE_ORDER,
            UIType.PAY_JUMP: AgentActionType.OPEN_PAYMENT,
            UIType.IMAGE_TASK: AgentActionType.START_IMAGE_TASK,
            UIType.DIALOG_OPTIONS: AgentActionType.SHOW_OPTIONS,
            UIType.TEXT: AgentActionType.SHOW_TEXT,
        }.get(ui, AgentActionType.SHOW_TEXT)
        capability = {
            AgentActionType.SHOW_PLAN: 'show_plan_page',
            AgentActionType.SHOW_SHOP: 'show_shop_page',
            AgentActionType.CREATE_ORDER: 'create_order',
            AgentActionType.OPEN_PAYMENT: 'open_payment',
            AgentActionType.START_IMAGE_TASK: 'start_image_task',
        }.get(action_type)
        action = AgentAction(
            type=action_type,
            payload={'reply': final_reply, 'ui': ui.value, 'data': data, 'stage': new_stage.value},
            required_capabilities=[capability] if capability else [],
            fallback=final_reply or '当前平台暂未实现对应能力，请使用文本方式继续引导。',
        )
        return ChatResponse(user_id=user_id, reply=final_reply, ui=ui, data=data, action=action, tool_calls=tool_log, session_id=sid, stage=new_stage.value, products=self._extract_products(tool_log))

    async def _post_process(
        self, respond_args, tool_log, incoming, message, final_reply,
        user_id, sid, location, new_msgs, session_req=None,
    ):
        """run() 的后处理：UI 推导、业务补调（生图/推方案/QA 过滤）、回复清理。

        Args:
            session_req: 本会话累积的结构化需求（跨轮合并结果），供 L3 追问兜底判据使用。
        """
        # ── 1. 推导 new_stage / ui / data ──
        if respond_args is not None:
            new_stage = self._derive_focus(tool_log, incoming, message)
            if new_stage == SessionStage.DONE:
                ordered = [tc.name for tc in tool_log if tc.status == 'ok']
                if 'create_order' not in ordered:
                    new_stage = SessionStage.SHOP_RECOMMEND if _entity_query_ok(tool_log, 'shop') else incoming
            ui_arg = str(respond_args.get('ui', ''))
            try:
                ui = UIType(ui_arg)
            except ValueError:
                ui = UIType.TEXT
            data_arg = respond_args.get('data') or {}
            data = data_arg if isinstance(data_arg, dict) else {}
            if self._validate_respond_data(ui, data) is None:
                data = {}
            inferred_ui, inferred_data = self._derive_ui(tool_log, new_stage, final_reply)
            _card_types = {UIType.DIALOG_OPTIONS, UIType.PLAN_CARD, UIType.SHOP_CARD, UIType.ORDER_CARD, UIType.PAY_JUMP}
            _data_effective = bool(data) and (not (ui == UIType.IMAGE_TASK and (not (data.get('task_id') or data.get('result_url')))))
            if not _data_effective:
                if inferred_ui in _card_types and inferred_data:
                    ui = inferred_ui
                    data = inferred_data
            elif ui == UIType.PLAN_CARD and inferred_ui == UIType.PLAN_CARD and inferred_data:
                ui = inferred_ui
                data = inferred_data
            if inferred_ui in (UIType.ORDER_CARD, UIType.PAY_JUMP) and inferred_data.get('pay_jump'):
                ui = UIType.PAY_JUMP
                data = inferred_data
            # 方案卡 + 生图任务并存：卡片是主 UI（花材 / 价格明细优先级更高），生图任务信息
            # 挂到卡片 data 上，前端据此轮询并在出图后补图。
            # 为什么（2026-09-16 线上实测）：模型出方案后会顺手调 generate_effect_image，
            # 旧逻辑让生图顶掉了方案卡 —— 用户只看到「效果图正在生成」，拿不到任何方案明细。
            _img_task = _extract_image_task(tool_log)
            if _img_task.get('task_id') and ui in (UIType.PLAN_CARD, UIType.SHOP_CARD, UIType.GREETING_CARD):
                data = {**data, **_img_task}
            if inferred_data.get('task_id'):
                if inferred_data.get('result_url'):
                    ui = UIType.IMAGE_TASK
                    data = {'task_id': inferred_data['task_id'], 'poll': inferred_data.get('poll'), 'result_url': inferred_data['result_url']}
                else:
                    ui = UIType.TEXT
                    data = {'task_id': inferred_data['task_id'], 'poll': inferred_data.get('poll')}
            final_reply = str(respond_args.get('reply', final_reply) or final_reply)
            if not final_reply.strip():
                final_reply = '我已经为你整理好相关结果啦，请查看下方卡片～' if tool_log else '好的，收到你的想法啦，请稍等～'
            if ui == UIType.DIALOG_OPTIONS and isinstance(data.get('options'), list):
                data['options'] = [o if isinstance(o, dict) and o.get('label') else {'label': str(o), 'value': str(o)} for o in data['options']]
            if ui == UIType.IMAGE_TASK:
                if inferred_data.get('task_id'):
                    data = {'task_id': inferred_data['task_id'], 'poll': inferred_data.get('poll')}
                    if inferred_data.get('result_url'):
                        data['result_url'] = inferred_data['result_url']
                else:
                    ui = UIType.TEXT
                    data = {}
            if ui == UIType.SHOP_CARD:
                if inferred_ui == UIType.SHOP_CARD and inferred_data.get('shops'):
                    ui = inferred_ui
                    data = inferred_data
                else:
                    ui = UIType.TEXT
                    data = {}
        else:
            new_stage = self._derive_focus(tool_log, incoming, message)
            if new_stage == SessionStage.DONE:
                ordered = [tc.name for tc in tool_log if tc.status == 'ok']
                if 'create_order' not in ordered:
                    new_stage = SessionStage.SHOP_RECOMMEND if _entity_query_ok(tool_log, 'shop') else incoming
            ui, data = self._derive_ui(tool_log, new_stage, final_reply)

        llm_intent = str(respond_args.get('intent', '') or '') if respond_args else ''
        # 本轮「对话理解」结构化信号：LLM 主导判断，关键词仅在其缺失/非法时兜底。
        # 注意解析必须早于下面各处消费，且 _affirmed 已内含「否定词一票否决」护栏。
        _confirm_signal = _signal_arg(respond_args, 'confirmation', _CONFIRM_SIGNALS)
        _img_signal = _signal_arg(respond_args, 'image', _IMAGE_SIGNALS)
        _affirmed = _resolve_affirmative(message, _confirm_signal)
        _img_want, _img_declined = _resolve_image(message, _img_signal)

        # ── 2. 图片确认标记 + 「拒绝生图」的会话级粘性标记 ──
        # img_optout 用 `img_` 前缀，**不会被 clear_session_flags(prefix='image_') 清掉**：
        # 用户说「不要效果图」后，即使之后产出新方案也不再自动生图，除非用户又明确要。
        # 修「拒了又硬塞」——此前新方案会无条件重新触发生图，把用户的拒绝冲掉。
        # L1 起：拒绝/想要由「模型信号 ∪ 关键词」判定（decline 取并集，最保守）。
        if _img_declined:
            await mem_store.set_session_flag(user_id, sid, 'img_optout', '1')
            await mem_store.clear_session_flags(user_id, sid, prefix='image_')
        elif _img_want:
            # 用户又明确要图了 → 撤销退出标记
            await mem_store.clear_session_flags(user_id, sid, prefix='img_optout')
            await mem_store.clear_session_flags(user_id, sid, prefix='image_')
        _img_optout = await mem_store.get_session_flag(user_id, sid, 'img_optout') == '1'
        if new_stage == SessionStage.IMAGE_GEN and new_stage != incoming:
            await mem_store.clear_session_flags(user_id, sid, prefix='image_')
            await mem_store.set_session_flag(user_id, sid, 'image_confirmed', '1')
        elif (not _img_optout) and _img_want and incoming in (SessionStage.DIY_DESIGN, SessionStage.IMAGE_GEN) and (await mem_store.get_session_flag(user_id, sid, 'image_confirmed') != '1'):
            await mem_store.set_session_flag(user_id, sid, 'image_confirmed', '1')

        # ── 3. 方案确认入库 ──
        # L1：以模型 confirmation 信号为主判据（能理解「好的，就这样吧」这类无「方案」字样的确认），
        # 显式短语与「方案」关键词兜底；_affirmed 已含「否定词一票否决」护栏——
        # 修既有缺陷：裸「这个方案」子串匹配会让「这个方案不行」也触发入库。
        if _affirmed and (any(w in message for w in _PLAN_CONFIRM_PHRASES) or _confirm_signal == 'confirm' or '方案' in message):
            try:
                from backend.storage.diy import save_diy_plan
                _diy = await mem_store.get_session_json(user_id, sid, 'latest_diy_plan')
                if _diy and _diy.get('diy'):
                    if not (_diy.get('result_url') or _diy.get('effect_image_url')):
                        try:
                            from backend.storage.tasks import get_image_task
                            for _m in reversed(await mem_store.load_display_messages(sid)):
                                _d = _m.get('data') if isinstance(_m.get('data'), dict) else {}
                                if _d.get('task_id'):
                                    _t = await get_image_task(str(_d['task_id']))
                                    if _t.get('result_url'):
                                        _diy['result_url'] = _t['result_url']
                                    break
                        except Exception:
                            logger.exception('[agent] DIY 方案效果图回填失败')
                    _diy['requirement'] = message
                    _res = await save_diy_plan(_diy, user_id)
                    logger.info('[agent] DIY 方案入库 saved=%s duplicate=%s id=%s', _res['saved'], _res['duplicate'], _res['plan_id'])
                    # L2 成交即学：用户确认的方案立即计入 proven 信号（confirm_count+1），
                    # 无需再手动跑 learn_from_history.py；不影响主流程，失败仅告警。
                    try:
                        from backend.storage.diy import record_plan_confirmed
                        if _res.get('plan_id'):
                            record_plan_confirmed(_res['plan_id'])
                    except Exception:  # noqa: BLE001
                        logger.warning('[agent] 方案确认学习信号记录失败', exc_info=True)
            except Exception:
                logger.exception('[agent] DIY 方案入库失败')

        # ── 4. 用户想「换一批」时清掉方案推送标志 ──
        # 注：曾有一版「兜底推方案」的硬编码逻辑，现已移除；这里只保留标志清理。
        # （原先残留的 _had_card / _plan_pushed 两个变量计算后从未被使用，已删除。）
        if _wants_alternative(respond_args, message):
            await mem_store.clear_session_flags(user_id, sid, prefix='plan_')

        # ── 5. QA 意图过滤 ──
        if llm_intent:
            _qa_intent = llm_intent == 'qa'
        else:
            _qa_intent = bool(re.search('什么|怎么|为什么|多久|花期|养护|寓意|百科|介绍|季节', message)) and (not any(w in message for w in ('买', '送', '预算', '下单', 'diy', '方案', '推荐', '想要', '需要', '束')))
        _has_plan_evidence = any(tc.name in ('generate_diy_plan', 'revise_diy_plan', 'generate_effect_image', 'create_order') and tc.status == 'ok' for tc in tool_log) or _entity_query_ok(tool_log, 'plan') or _entity_query_ok(tool_log, 'shop')
        if _qa_intent and ui == UIType.PLAN_CARD and (not _has_plan_evidence):
            ui = UIType.TEXT
            data = {}
            logger.info('[agent] 知识问答轮次，丢弃 LLM 擅自推送的方案卡')

        # ── 5.5 L3 澄清式追问：信息不足 → 先问清楚，不推猜出来的方案卡 ──
        # 模型若自报 missing（缺关键信息）却仍产出了 DIY 方案卡，这里**确定性地**拦下：
        # 卡片降级为文字 + 追加追问句；同一需求内只问一次；用户说「随便/你决定」则豁免。
        _diy_produced = any(tc.name in ('generate_diy_plan', 'revise_diy_plan') and tc.status == 'ok' for tc in tool_log)
        _clarify = _clarify_slots(
            message, respond_args, ui, _diy_produced,
            asked_before=await mem_store.get_session_flag(user_id, sid, 'clarify_asked') == '1',
            session_req=session_req,
        )
        _clarified = bool(_clarify)
        if _clarified:
            await mem_store.set_session_flag(user_id, sid, 'clarify_asked', '1')
            ui = UIType.TEXT
            data = {}
            final_reply = _append_clarify(final_reply, _clarify)
            logger.info('[agent] L3 澄清追问 slots=%s（本轮不推方案卡）', _clarify)
        elif (isinstance(respond_args, dict) and respond_args.get('missing')
              and ui != UIType.PLAN_CARD):
            # 模型**自己**用文字追问（没产出卡片）时同样要记「已追问过一次」。
            # 否则「追问只做一次、第二次必须给方案」这条规则没有落点：线上实测
            # （2026-09-16）模型第 1 轮纯文字追问没有留下标记 → 第 2 轮用户补齐后
            # 模型既不追问也不出卡，改成用文字写了一段"凭想象"的方案。
            await mem_store.set_session_flag(user_id, sid, 'clarify_asked', '1')
            logger.info('[agent] 模型自报缺信息 %s，标记本会话已追问一次', respond_args.get('missing'))

        # ── 6. 方案即生图 ──
        diy_done = _diy_produced
        eff_done = any(tc.name == 'generate_effect_image' and tc.status == 'ok' for tc in tool_log)
        # 产出新方案 → 清掉上一张图的补调标记，让新方案能重新触发一次生图
        if diy_done:
            await mem_store.clear_session_flags(user_id, sid, prefix='image_')
            # 本轮没有追问 = 需求已完整成立，允许将来（新需求）再追问一次
            if not _clarified:
                await mem_store.clear_session_flags(user_id, sid, prefix='clarify_')
        if diy_done and (not eff_done) and (ui == UIType.PLAN_CARD) and (not _img_optout) and (new_stage not in (SessionStage.DONE, SessionStage.ORDER_CONFIRM)):
            try:
                from agent.tools import generate_effect_image as _gei
                await mem_store.set_session_flag(user_id, sid, 'image_confirmed', '1')
                raw = await _gei('latest_diy', {'user_id': user_id, 'session_id': sid, 'location': location})
                eff = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                if 'task_id' in eff:
                    data = {**data, 'task_id': eff['task_id'], 'poll': eff.get('poll', True)}
                    if eff.get('result_url'):
                        data['result_url'] = eff['result_url']
                    tool_log.append(ToolCallRecord(name='generate_effect_image', arguments={'plan': 'latest_diy'}, result=json.dumps(eff, ensure_ascii=False), status='ok'))
                    logger.info('[agent] 方案即生图 task_id=%s', eff['task_id'])
            except Exception:
                logger.exception('[agent] 方案即生图失败')

        # ── 7. 生图补调（幂等 + 去重 + 意图豁免）──
        # 历史坑：这里曾因「标志永不清除 + 不去重 + 硬编码覆盖回复」造成死循环——
        # 用户说什么都回同一句「正在为您生成效果图预览」，且每句话都新建一个生图任务烧 API。
        eff_confirmed = await mem_store.get_session_flag(user_id, sid, 'image_confirmed') == '1'
        eff_forced = await mem_store.get_session_flag(user_id, sid, 'image_forced') == '1'
        eff_done = any(tc.name == 'generate_effect_image' and tc.status == 'ok' for tc in tool_log)
        # 本轮模型**自己**真的调了生图 → 同样标记「本会话已出过图」。否则下一轮只要 ui 不是
        # plan_card 就会再补调一次；线上实测（2026-09-16）：用户随后问「数据库包括哪些」，
        # 系统又白烧一次生图 API（image_forced 原先只在「补调成功」时置位，漏了这条路径）。
        if eff_done:
            await mem_store.set_session_flag(user_id, sid, 'image_forced', '1')

        if _img_declined:
            # 意图豁免：用户明确不要生图（模型信号 ∪ 关键词）→ 记**粘性**退出标记 img_optout
            # + 清 image_ 标志，本轮及以后都不再补调（除非用户又明确要图，见第 2 段）。
            await mem_store.set_session_flag(user_id, sid, 'img_optout', '1')
            await mem_store.clear_session_flags(user_id, sid, prefix='image_')
            logger.info('[agent] 用户拒绝生图，记录 img_optout 并停止补调')
        elif (eff_confirmed and (not eff_done) and (not eff_forced) and (not _img_optout)
              and (ui != UIType.PLAN_CARD)
              # L3：本轮若在追问（还没有可信方案），就不要拿上一轮的旧方案去生图
              and (not _clarified)
              and (new_stage not in (SessionStage.DONE, SessionStage.ORDER_CONFIRM))):
            eff: dict[str, Any] = {}
            # 去重：会话里已有正在生成的任务就复用，绝不重复烧生图 API
            pending = await _find_pending_image_task(user_id, sid)
            if pending and pending.get('task_id'):
                eff = {'task_id': pending['task_id'], 'poll': True, 'reused': True}
                logger.info('[agent] 生图补调复用未完成任务 task_id=%s', eff['task_id'])
            else:
                try:
                    from agent.tools import generate_effect_image as _gei
                    await mem_store.update_stage(sid, SessionStage.IMAGE_GEN.value)
                    raw = await _gei('latest_diy', {'user_id': user_id, 'session_id': sid, 'location': location})
                    eff = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                    logger.info('[agent] 生图补调新建任务 task_id=%s', eff.get('task_id'))
                except Exception:
                    logger.exception('[agent] 生图补调失败')
            if 'task_id' in eff:
                # 幂等：标记本会话已补调过，避免之后每轮重复触发
                await mem_store.set_session_flag(user_id, sid, 'image_forced', '1')
                # 回复主体必须回应用户的发言：生图提示只作追加，绝不顶替模型原答复
                # （历史坑：曾直接覆盖成固定文案，导致用户说什么都得到同一句）
                _notice = '（效果图我在生成中，稍等一下就好～）'
                if (final_reply or '').strip():
                    if '效果图' not in final_reply:
                        final_reply = final_reply.rstrip() + _notice
                else:
                    final_reply = '好的～' + _notice
                data = {**(data or {}), 'task_id': eff['task_id'], 'poll': eff.get('poll', True)}
                if eff.get('result_url'):
                    data['result_url'] = eff['result_url']
                tool_log.append(ToolCallRecord(name='generate_effect_image', arguments={'plan': 'latest_diy'}, result=json.dumps(eff, ensure_ascii=False), status='ok'))
                new_msgs.append({'role': 'tool', 'content': json.dumps(eff, ensure_ascii=False), 'tool_call_id': 'forced_effect_image'})

        # ── 8. 回复清理 ──
        final_reply = _clean_reply(final_reply)
        if ui in (UIType.ORDER_CARD, UIType.PAY_JUMP):
            final_reply = re.sub('[，,]?共\\s*\\d+[\\d.]*\\s*元', '', final_reply)
            final_reply = re.sub('[，,]?\\d+[\\d.]*\\s*元[。.?]', '', final_reply)
            final_reply = final_reply.strip() or '订单已生成，请确认信息后去支付～'
        if ui.value != UIType.TEXT and (not final_reply or final_reply == '好的，收到你的想法啦，请稍等～'):
            final_reply = ''
        elif not final_reply:
            final_reply = '我已经为你整理好相关结果啦，请查看下方卡片～' if tool_log else '好的，收到你的想法啦，请稍等～'

        return new_stage, ui, data, final_reply, llm_intent

    def _build_system(self, stage: SessionStage, long_term: dict[str, str], shop_id: str | None=None, entry: str | None=None, product_id: str | None=None, product_title: str | None=None, current_plan: str='', platform_facts: str='') -> str:
        """构造 system prompt：身份 + 能力 + 工具，鼓励自主推理。

        entry 决定会话模式：product/shop（已选定商品或店铺）→ 店铺锁定；home → 全平台。
        platform_facts 非空（'shop'/'plan'）表示**本轮**在问平台事实类信息，追加
        「必须先查证再回答」的按轮指令（防编造店铺名/价格，见 _platform_fact_hint）。
        """
        parts = [_render_prompt('base', current_time=_now_context())]

        sources = _platform_source_ids()
        if sources:
            parts.append(_render_prompt('platform_sources', sources='、'.join(sources)))
        else:
            parts.append(_load_prompt('platform_none'))

        # 店铺链路关闭时不再按轮注入「先查店铺再回答」的指令（schema 也不暴露 shop）。
        if platform_facts and sources and not (platform_facts == 'shop' and not _shop_entity_enabled()):
            label = ('店铺（名称/营业时间/配送/地址/电话/评分等）' if platform_facts == 'shop'
                     else '在售商品/方案（名称/价格/花材构成等）')
            parts.append(_render_prompt(
                'platform_facts_turn',
                entity_label=label, entity=platform_facts, sources='、'.join(sources),
            ))

        if stage == SessionStage.IMAGE_GEN:
            parts.append(_load_prompt('stage_image_gen'))

        if long_term:
            mem = '；'.join((f'{k}={v}' for k, v in long_term.items()))
            parts.append('## 用户偏好记忆：' + mem)

        entry = normalize_entry(entry, shop_id, product_id)
        if shop_id and _shop_entity_enabled():
            origin = '从商品详情页进入，该商品归属' if entry == 'product' else '从店铺详情页进入，'
            parts.append(_render_prompt('shop_lock', origin=origin, shop_id=shop_id))
        else:
            # 体验版（不开放店铺）换「只做方案与建议」的变体，否则 prompt 会引导模型去查店铺，
            # 而 schema 里已经没有 shop → 白跑一轮还被执行层拒。
            parts.append(_load_prompt('full_platform' if _shop_entity_enabled() else 'full_platform_plan_only'))

        if entry == 'product' and (product_id or product_title):
            # 用户从商品详情页进入：智能体必须知道「用户此刻在看哪件商品」，
            # 否则用户问「这个多少钱 / 今天能送到吗」时模型会反问「你说的是哪个」。
            parts.append(_render_prompt(
                'product_context',
                id_hint=f'（商品 ID：{product_id}）' if product_id else '',
                shown=f'「{product_title}」' if product_title else '该商品',
                product_id=product_id or '',
                fallback=(
                    f'该 id 查不到时再退化用 keyword="{product_title}" 按名称查，并如实说明查不到详情。'
                    if product_title else '查不到时如实说明，不要编造商品信息。'
                ),
                shop_clause=f'在本店（{shop_id}）范围内' if shop_id else '',
            ))

        # 会话已产出方案时，把方案要点作为**权威数据**注入（放在最后，靠近用户消息，
        # 降低模型凭记忆复述方案导致的花材/配色失真；见 _latest_plan_summary 背景说明）。
        if current_plan:
            parts.append(_render_prompt('current_plan', plan_summary=current_plan))

        # 说明：不再注入「## 工具说明书」段 —— 工具定义已由 function-calling 的 tools 参数
        # 完整提供（含每个参数的 JSON Schema），prompt 内再写一份纯属重复，且信息更少。
        # 经 A/B 实测（2026-09-11）移除后工具选择无退化，输入字符 -29.6%。
        # generate_tool_manual() 保留在 toolkit.py，作为「provider 不支持 function calling」时的文本兜底。
        return '\n\n'.join(parts)

    @staticmethod
    def _stream_llm(messages: list[dict[str, Any]], on_event: Callable[[dict], None],
                    remaining: float) -> Any:
        """流式调用 LLM，把**面向用户的文字**实时推给前端。

        为什么（2026-09-18 外部安全审计 P0-1）：单轮 25–92s，用户全程只看到转圈。
        SSE 此前只推「工具开始/结束 + 最终整段」，90 秒也就 2 个整段，观感极差。

        ⚠️ 关键坑（本次实测发现，必须处理）：**模型在工具轮会先输出一大段"自言自语"
        再调工具**，例如「我注意到工具要求必须提供 source_id 参数，但我没有这个信息。
        …让他们选择。」——这段 content 不是给用户看的。若边收边推，等于把内部独白
        **实时直播**给用户，比现在（被 `_finalize_reasoning_leak` 拦下）更糟。

        因此设了三层保护（全部带回滚）：
        1. **头部缓冲** `_HEAD_BUFFER` 个字符后才开始推：纯回复轮只是推迟几十字，
           工具轮的构思文字则有机会在推送前先撞上 `tool_calls` 被整段丢弃；
        2. **遇到 `tool_calls`** → 丢弃已缓冲内容；若已经推过则发 `text_rollback` 让前端撤回；
        3. **独白实时检测** → 累计内容命中 `_looks_like_reasoning_leak` 即停止推送并回滚。

        最终以 `done` 事件里**经清理链处理过**的 `reply` 为准（接入方应在 done 时覆盖）。

        Args:
            messages: 本轮 LLM 上下文。
            on_event: SSE 事件回调。
            remaining: 本轮剩余时间预算（秒）。

        Returns:
            与 ``resp.choices[0].message`` 等价的对象（``content`` + ``tool_calls``）。
        """
        _HEAD_BUFFER = 40
        stream = call_llm_stream(messages, tools=to_openai_tools(),
                                 timeout=min(settings.llm_timeout, remaining))
        content_buf = ''
        pushed = False          # 是否已向用户推送过
        push_enabled = True     # 命中独白/工具轮后置 False
        saw_tool_call = False
        tc_acc: dict[int, dict[str, str]] = {}
        _reply_pushed = 0       # 终结工具 reply 参数已推送的字符数（增量推送用）

        for _n_chunks, chunk in enumerate(stream, 1):
            choices = getattr(chunk, 'choices', None)
            if not choices:
                continue
            delta = getattr(choices[0], 'delta', None)
            if delta is None:
                continue

            tcs = getattr(delta, 'tool_calls', None)
            if tcs:
                if not saw_tool_call:
                    saw_tool_call = True
                    content_buf = ''            # 工具轮的 content 是构思，丢弃
                    push_enabled = False
                    if pushed:
                        on_event({'event': 'text_rollback'})
                        pushed = False
                for t in tcs:
                    idx = getattr(t, 'index', 0) or 0
                    slot = tc_acc.setdefault(idx, {'id': '', 'name': '', 'arguments': ''})
                    if getattr(t, 'id', None):
                        slot['id'] = t.id
                    fn = getattr(t, 'function', None)
                    if fn is not None:
                        if getattr(fn, 'name', None):
                            slot['name'] = fn.name
                        if getattr(fn, 'arguments', None):
                            slot['arguments'] += fn.arguments
                # 终结工具的 `reply` 参数就是最终回复 —— 边收边推。
                # ⚠️ 这是真流式的**主战场**：实测模型最终回复的 content 为 0 字，
                # 全部文字都在 `respond_to_user` / `show_plan_card` 的 JSON 参数里。
                _slot = tc_acc.get(0)
                if _slot and _slot['name'] in ('respond_to_user', 'show_plan_card'):
                    _cur = _extract_partial_json_string(_slot['arguments'], 'reply')
                    if len(_cur) > _reply_pushed and not _has_process_narration(_cur):
                        on_event({'event': 'text_delta', 'content': _cur[_reply_pushed:]})
                        _reply_pushed = len(_cur)
                        pushed = True
                continue

            text = getattr(delta, 'content', None)
            if not text or not push_enabled:
                continue
            content_buf += text
            # 推送**之前**先粗筛「过程说明 / 内部独白」：命中就本轮不再推。
            # ⚠️ 必须在推送前判——等推出去再回滚，用户已经看到那段自言自语了
            # （实测：模型工具轮的自言自语可达几十字，超过头部缓冲）。
            if _has_process_narration(content_buf):
                if pushed:
                    on_event({'event': 'text_rollback'})
                    pushed = False
                push_enabled = False
                logger.warning('[agent] 流式推送前检测到过程说明/独白，本轮停止推送')
                continue
            if not pushed and len(content_buf) >= _HEAD_BUFFER:
                pushed = True
                on_event({'event': 'text_delta', 'content': content_buf})
            elif pushed:
                on_event({'event': 'text_delta', 'content': text})

        logger.info('[agent] 流式调用结束：chunks=%d content=%d字 pushed=%s tools=%d',
                    _n_chunks, len(content_buf), pushed, len(tc_acc))
        calls = None
        if tc_acc:
            calls = [
                SimpleNamespace(
                    id=v['id'] or f'call_{i}',
                    type='function',
                    function=SimpleNamespace(name=v['name'], arguments=v['arguments'] or '{}'),
                )
                for i, v in sorted(tc_acc.items())
            ]
        # `_pushed` 供主循环判断"是否已逐字推过"：已推过就不再走伪流式整段推，
        # 否则用户会看到同一段文字出现两遍。
        return SimpleNamespace(content=content_buf, tool_calls=calls, _pushed=pushed)

    @staticmethod
    def _parse_tool_calls(msg: Any) -> list[dict[str, Any]]:
        """兼容 OpenAI（msg.tool_calls[i].function）与 Mock（_MockToolCall）。

        模型的工具参数 JSON 可能被截断 / 写坏——**必须兜住**：坏掉的那一条直接跳过
        （等价于模型没调用该工具），并告警。否则 `json.loads` 抛异常会**整轮崩掉**。
        """
        raw = getattr(msg, 'tool_calls', None)
        if not raw:
            return []
        calls: list[dict[str, Any]] = []
        for tc in raw:
            fn = getattr(tc, 'function', None)
            name = getattr(fn, 'name', '') if fn is not None else ''
            if not name:
                continue
            raw_args = getattr(fn, 'arguments', '') or '{}'
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                logger.warning('[agent] 工具调用参数非法 JSON，已跳过：name=%s args=%.200s', name, raw_args)
                continue
            if not isinstance(args, dict):
                args = {}
            calls.append({'id': getattr(tc, 'id', ''), 'name': name, 'arguments': args})
        return calls

    @staticmethod
    def _derive_focus(tool_log: list[ToolCallRecord], incoming: SessionStage, message: str) -> SessionStage:
        """基于本轮工具产出推导 UI 焦点（focus），不再做状态机拦截。

        skill 编排模式下，focus 仅用于前端高亮「用户当前在做什么」，不限制流程：
        - 有订单 → done；电子贺卡渲染 → greeting_card；
        - 店铺查询(platform_db_query_entity entity=shop) → shop_recommend；
        - 有生图 → image_gen；DIY 方案生成/改版 → diy_design；
        - 平台在售方案浏览(entity=plan) → view_plan；
        - 否则保持进入时的焦点（incoming），避免无工具轮次焦点乱跳。
        """
        ordered = [tc.name for tc in tool_log if tc.status == 'ok']
        if 'create_order' in ordered:
            return SessionStage.DONE
        if 'render_greeting_card' in ordered:
            return SessionStage.GREETING_CARD
        if _entity_query_ok(tool_log, 'shop'):
            return SessionStage.SHOP_RECOMMEND
        if 'generate_effect_image' in ordered:
            return SessionStage.IMAGE_GEN
        if 'generate_diy_plan' in ordered or 'revise_diy_plan' in ordered:
            return SessionStage.DIY_DESIGN
        if _entity_query_ok(tool_log, 'plan'):
            return SessionStage.VIEW_PLAN
        return incoming

    @staticmethod
    def _validate_respond_data(ui: UIType, data: dict) -> dict | None:
        """按 ui 契约校验 respond_to_user 携带的 data 形状；无效返回 None。

        卡片类 ui 必须有核心字段，否则视为 LLM 幻觉（如 plan_card 无 plans、
        pay_jump 无 order_id、dialog_options 无 options），调用方据此把 data 置空，
        交给 _derive_ui 依据真实工具成果重建卡片。
        """
        if ui == UIType.TEXT:
            return data
        if ui == UIType.DIALOG_OPTIONS:
            return data if isinstance(data.get('options'), list) and data['options'] else None
        if ui == UIType.PLAN_CARD:
            return data if isinstance(data.get('plans'), list) and data['plans'] else None
        if ui == UIType.SHOP_CARD:
            return data if isinstance(data.get('shops'), list) and data['shops'] else None
        if ui in (UIType.ORDER_CARD, UIType.PAY_JUMP):
            return data if data.get('order_id') or data.get('page_path') else None
        if ui == UIType.IMAGE_TASK:
            return data if data.get('task_id') or data.get('result_url') else None
        if ui == UIType.GREETING_CARD:
            return data if data.get('image_url') else None
        return data

    def _derive_ui(self, tool_log: list[ToolCallRecord], stage: SessionStage, reply: str) -> tuple[UIType, dict[str, Any]]:
        """根据本轮工具产出决定 ui 类型与 data。

        注意：不能只看「最后一个成功工具」——live LLM 常在设计/生图之后追加
        save_memory / retrieve_knowledge 落库偏好，若只取 last 会漏掉方案卡/生图卡/
        店铺卡（已复现：generate_diy_plan > save_memory 时 ui 退化为 text）。
        这里从最近一次成功工具回溯，跳过不产出卡片的辅助工具，命中即返回。
        """
        # 卡片类：本轮的**主输出**。
        card_renderers: dict[str, Callable[[dict[str, Any]], tuple[UIType, dict[str, Any]]]] = {
            'generate_diy_plan': lambda r: (UIType.PLAN_CARD, {'plans': [r]}),
            'revise_diy_plan': lambda r: (UIType.PLAN_CARD, {'plans': [r]}),
            'create_order': lambda r: (UIType.ORDER_CARD, r),
            'render_greeting_card': lambda r: (UIType.GREETING_CARD, r),
        }
        # 生图是**补充产出**，单独一轮、后扫。为什么（2026-09-16 线上实测）：模型出方案后
        # 常顺手调 generate_effect_image，若与卡片混在同一轮「取最近一次产卡工具」，生图
        # （排在方案之后）会顶掉方案卡 —— 用户只看到「效果图正在生成」，拿不到花材与价格明细。
        image_renderers: dict[str, Callable[[dict[str, Any]], tuple[UIType, dict[str, Any]]]] = {
            'generate_effect_image': lambda r: (UIType.IMAGE_TASK, {'task_id': r.get('task_id'), 'poll': r.get('poll'), **({'result_url': r['result_url']} if r.get('result_url') else {})}),
        }
        for renderers in (card_renderers, image_renderers):
            for tc in reversed(tool_log):
                if tc.status != 'ok':
                    continue
                try:
                    result = json.loads(tc.result) if isinstance(tc.result, str) else tc.result or {}
                except (json.JSONDecodeError, TypeError):
                    result = {}
                if isinstance(result, dict) and result.get('error'):
                    # 工具自身返回 {error}（如下单/渲染失败）：不产卡片，回退 text 如实播报
                    continue
                # platform_db_query_entity 返回 tool_result 封装 {ok, data:[规范行], error}，
                # 按调用参数的 entity 分流：plan → plan_card、shop → shop_card；失败/空结果不产卡片。
                if tc.name == 'platform_db_query_entity':
                    if renderers is not card_renderers:
                        continue
                    if not isinstance(result, dict) or result.get('ok') is not True:
                        continue
                    rows = result.get('data')
                    if not isinstance(rows, list) or not rows:
                        continue
                    entity = (tc.arguments or {}).get('entity')
                    if entity == 'plan':
                        # ⚠️ 此处**不**做去重/文案对齐：_derive_ui 被调用时 reply 还没定型
                        # （respond_to_user 的 reply 更晚才覆盖 final_reply），提前过滤+截断会把
                        # 真正推荐的商品挤掉。统一交给 run() 末尾的 _align_card_data_with_reply。
                        return (UIType.PLAN_CARD, {'plans': rows})
                    if entity == 'shop':
                        # 双保险：执行层已按白名单拒绝店铺查询，这里确保也不会漏出店铺卡。
                        if not _shop_entity_enabled():
                            continue
                        return (UIType.SHOP_CARD, {'shops': rows})
                    continue
                render = renderers.get(tc.name)
                if not render:
                    continue
                if isinstance(result, list) and (not result):
                    continue
                return render(result)
        return (UIType.TEXT, {})

    # 商品卡展示上限：平台一次查询常回 20 行，全倒出来既啰嗦又把文案冲淡。
    PRODUCT_CARD_LIMIT = 8

    @classmethod
    def _align_products_with_reply(cls, rows: list[Any], reply: str) -> list[dict[str, Any]]:
        """把商品卡与回复文字对齐：去重 → 只留回复点名的 → 截断。

        Args:
            rows: `platform_db_query_entity(entity='plan')` 返回的原始行。
            reply: 本轮最终回复文字（用于判断模型推荐了哪几款）。

        Returns:
            可直接渲染的商品行列表。

        为什么（2026-09-16 线上实测）：平台 `products` 里同款花束会在多家店铺各自上架，
        原始查询一次返回 20~50 行，其中「感恩母亲 ¥158」重复出现 6 次以上；而且**同款在不同
        店铺定价还不一样**（实测「粉色梦境」s004=118 元 / s008=129 元），所以去重键只能取
        **花名**，不能带价格——否则用户照样看到两张「粉色梦境」。规则：

        1. **同名即同款**，保留平台排序靠前的那条（通常为主店铺）；
        2. 回复里**点名**了若干款时只展示这些（模型已替用户筛过一轮）；若一个都没匹配上
           （模型换了说法/用别名），退回去重后的全集——宁多勿漏，避免卡片空掉；
        3. 最多 `PRODUCT_CARD_LIMIT` 条。
        """
        uniq: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            name = str(r.get('name') or '').strip()
            if not name or name in seen:
                continue
            seen.add(name)
            uniq.append(r)
        if not uniq:
            return []
        if reply:
            picked = [r for r in uniq if str(r.get('name') or '').strip() in reply]
            if picked:
                uniq = picked
        return uniq[:cls.PRODUCT_CARD_LIMIT]

    def _extract_products(self, tool_log: list[ToolCallRecord]) -> list[dict[str, Any]]:
        """从本轮工具日志提取 platform_db_query_entity(entity=plan) 的成功结果，

        规整为同事平台约定的 products 数组（plan_id/name/price_yuan/image/stock）。
        展示层转换已在 query_external_entity 完成（price→元、image→CDN URL），此处直接复用。
        """
        for tc in reversed(tool_log):
            if tc.status != 'ok' or tc.name != 'platform_db_query_entity':
                continue
            try:
                result = json.loads(tc.result) if isinstance(tc.result, str) else tc.result or {}
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(result, dict) or result.get('ok') is not True:
                continue
            if (tc.arguments or {}).get('entity') != 'plan':
                continue
            rows = result.get('data')
            if not isinstance(rows, list) or not rows:
                continue
            # 同样去重 + 截断：products 字段会直接展示给用户，不能带重复行。
            rows = self._align_products_with_reply(rows, '')
            if not rows:
                continue
            products: list[dict[str, Any]] = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                products.append({
                    'plan_id': str(r.get('id', '')),
                    'name': str(r.get('name', '')),
                    'price_yuan': r.get('price') or 0,
                    'image': str(r.get('image', '')),
                    'stock': r.get('stock') or 0,
                })
            return products
        return []

if __name__ == '__main__':
    setup_logging()
    from backend.storage.db import init_db
    init_db()
    agent = ReActAgent()
    user_msg = '想给母亲买一束花，预算 200 元左右'
    result = agent.run('cli_user', user_msg)
    print(result.model_dump_json(indent=2))
