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
from typing import Any

from agent.engine.llm import call_llm
from agent.engine.state import SessionStage
from agent.engine.ui_protocol import AgentAction, AgentActionType, ChatResponse, ToolCallRecord, UIType
from agent.ports import normalize_entry, normalize_product_id, normalize_product_title, normalize_shop_id
from agent.toolkit import execute_tool, to_openai_tools
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
    out = re.sub(r'\n{3,}', '\n\n', out).strip('\n')
    if dropped_payload:
        logger.warning('[agent] 回复中出现原始数据行，已移除（隐私兜底）')
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

    与 backend/data_gateway/external.py 一致：连接串从
    ``PLATFORM_DB_<SOURCE_ID>_URL`` 读取（部署方注入容器环境变量）。
    未配置任何平台库时返回空列表，供 system prompt 如实告知 LLM。
    """
    ids: set[str] = set()
    for key in os.environ:
        if key.startswith('PLATFORM_DB_') and key.endswith('_URL'):
            mid = key[len('PLATFORM_DB_'):-len('_URL')]
            if mid:
                ids.add(mid.lower())
    return sorted(ids)


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
    tail = f'在给你定方案之前，想先确认一下：{asks}？'
    base = (reply or '').strip()
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
        - {"event": "text", "content": "..."}  — 逐句输出最终回复
        - {"event": "card", "ui": "...", "data": {...}}  — 结构化卡片
        - {"event": "done", "session_id": "..."}
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
                    await queue.put({'event': 'done', 'session_id': result.session_id})
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
        incoming = stage
        # 此处早于 LLM 调用，拿不到本轮结构化信号，故仍用关键词做「上一轮已进入生图阶段
        # 且用户肯定」的快速标记（保守方向：只多标一次 image_confirmed，不触发生图）。
        # 主判据在 _post_process 段 2（_resolve_image：模型信号优先 + 关键词兜底）。
        if stage == SessionStage.IMAGE_GEN and is_affirmative(message):
            await mem_store.set_session_flag(user_id, sid, 'image_confirmed', '1')
        long_term = await mem_store.get_long_term(user_id)
        history = await mem_store.load_history(sid, settings.history_limit)
        system = self._build_system(stage, long_term, shop_id=shop_id, entry=entry, product_id=product_id, product_title=product_title)
        messages: list[dict[str, Any]] = [{'role': 'system', 'content': system}]
        messages += history
        messages.append({'role': 'user', 'content': message})
        tool_log: list[ToolCallRecord] = []
        respond_args: dict[str, Any] | None = None
        final_reply = ''
        new_msgs: list[dict[str, Any]] = [{'role': 'user', 'content': message}]
        for turn in range(1, settings.max_iterations + 1):
            _remaining = _deadline - time.perf_counter()
            if _remaining <= _MIN_ITER_BUDGET:
                logger.warning('[agent] 时间预算耗尽（剩余 %.1fs），第 %d 轮主动收尾', _remaining, turn)
                final_reply = final_reply or '这个问题有点绕，我先给你一个初步建议；要更细致的可以咱们分步慢慢聊～'
                break
            logger.info('[agent] ReAct 第 %d/%d 轮 阶段=%s', turn, settings.max_iterations, stage.value)
            try:
                # 按本轮剩余预算收紧单次 LLM 超时，避免「8 轮 × 3 重试 × 120s」把整轮拖长
                resp = call_llm(messages, tools=to_openai_tools(), timeout=min(settings.llm_timeout, _remaining))
            except Exception as exc:
                # 安全：原始异常只落服务端日志（含完整 traceback），绝不回显给用户，避免泄露
                # endpoint / 模型 ID / 密钥前缀 / 内部堆栈等敏感信息（K-1 修复）。
                logger.exception('[agent] LLM 调用失败')
                final_reply = '抱歉，我这边服务暂时开小差了，请稍后再试一次～'
                break
            msg = resp.choices[0].message
            tool_calls = self._parse_tool_calls(msg)
            if tool_calls:
                assistant_msg = {'role': 'assistant', 'content': getattr(msg, 'content', '') or '', 'tool_calls': [{'id': tc['id'], 'type': 'function', 'function': {'name': tc['name'], 'arguments': json.dumps(tc['arguments'], ensure_ascii=False)}} for tc in tool_calls]}
                messages.append(assistant_msg)
                new_msgs.append({**assistant_msg, 'content': ''})
                for tc in tool_calls:
                    if tc['name'] in ('respond_to_user', 'show_plan_card'):
                        respond_args = tc['arguments']
                        obs = json.dumps(respond_args, ensure_ascii=False)
                        messages.append({'role': 'tool', 'content': obs, 'tool_call_id': tc.get('id', '')})
                        new_msgs.append({'role': 'tool', 'content': obs, 'tool_call_id': tc.get('id', '')})
                        continue
                    # 注入会话累积需求：工具侧可用它补全跨轮信息（如 DIY 设计只传了「11朵粉玫瑰」，
                    # 前几轮说过的「送妈妈、预算200」仍生效）。
                    result, status = await execute_tool(tc['name'], tc['arguments'], {'user_id': user_id, 'session_id': sid, 'location': location, 'shop_id': shop_id, 'entry': entry, 'product_id': product_id, 'product_title': product_title, 'requirement': req_acc})
                    record = ToolCallRecord(name=tc['name'], arguments=tc['arguments'], result=result, status=status)
                    tool_log.append(record)
                    if on_event:
                        on_event({'event': 'tool_call', 'name': tc['name'], 'status': status})
                    messages.append({'role': 'tool', 'content': result, 'tool_call_id': tc.get('id', '')})
                    new_msgs.append({'role': 'tool', 'content': result, 'tool_call_id': tc.get('id', '')})
                if respond_args is not None:
                    break
                continue
            else:
                final_reply = getattr(msg, 'content', '') or ''
                messages.append({'role': 'assistant', 'content': final_reply})
                break
        else:
            if any(tc.status == 'ok' for tc in tool_log):
                final_reply = final_reply or '我已经为你整理好相关结果啦，请查看下方卡片～'
            else:
                final_reply = final_reply or '抱歉，我思考得太久啦，请简化需求或分步骤再问我～'
        new_stage, ui, data, final_reply, llm_intent = await self._post_process(
            respond_args, tool_log, incoming, message, final_reply, user_id, sid, location, new_msgs,
            session_req=req_acc,
        )
        # 排版兜底：删掉 LLM 回复里独立的分隔线行（--- / *** / ——），改为靠 prompt
        # 规则让其用数字编号分段；此处只做删除不做改写，不碰卡片数据。
        final_reply = _strip_separator_lines(final_reply)
        # 隐私兜底：去掉回复里残留的工具名 / 内部字段 / 原始数据行，
        # 确保数据库查询结果与内部实现不出现在前端（prompt 约束 + 代码兜底双保险）。
        final_reply = _strip_internal_leak(final_reply)
        # 要点兜底：带卡片但回复过短时，追加一句基于卡片数据的可读要点
        # （模型有卡片时倾向只说「看卡片」，prompt 约束不稳定，此处确定性补齐）。
        final_reply = _ensure_card_summary(final_reply, ui, data)
        # （原先此处重复计算过一个 _img_intent，从未被使用——生图意图判断已统一在
        #   _post_process 内经 _resolve_image 消费结构化信号，故删除。）
        new_msgs.append({'role': 'assistant', 'content': final_reply, 'ui': ui.value, 'data': data})
        await mem_store.save_messages(sid, new_msgs)
        await mem_store.update_stage(sid, new_stage.value)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info('[agent] 完成 阶段=%s ui=%s 耗时=%.0fms', new_stage.value, ui.value, elapsed)
        if on_event:
            import re as _re
            parts = _re.split('([。！？\\n])', final_reply or '')
            buf = ''
            for seg in parts:
                buf += seg
                if seg in ('。', '！', '？', '\n') or len(buf) > 20:
                    on_event({'event': 'text', 'content': buf})
                    buf = ''
                    time.sleep(0.03)
            if buf:
                on_event({'event': 'text', 'content': buf})
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

    def _build_system(self, stage: SessionStage, long_term: dict[str, str], shop_id: str | None=None, entry: str | None=None, product_id: str | None=None, product_title: str | None=None) -> str:
        """构造 system prompt：身份 + 能力 + 工具，鼓励自主推理。

        entry 决定会话模式：product/shop（已选定商品或店铺）→ 店铺锁定；home → 全平台。
        """
        parts = [_render_prompt('base', current_time=_now_context())]

        sources = _platform_source_ids()
        if sources:
            parts.append(_render_prompt('platform_sources', sources='、'.join(sources)))
        else:
            parts.append(_load_prompt('platform_none'))

        if stage == SessionStage.IMAGE_GEN:
            parts.append(_load_prompt('stage_image_gen'))

        if long_term:
            mem = '；'.join((f'{k}={v}' for k, v in long_term.items()))
            parts.append('## 用户偏好记忆：' + mem)

        entry = normalize_entry(entry, shop_id, product_id)
        if shop_id:
            origin = '从商品详情页进入，该商品归属' if entry == 'product' else '从店铺详情页进入，'
            parts.append(_render_prompt('shop_lock', origin=origin, shop_id=shop_id))
        else:
            parts.append(_load_prompt('full_platform'))

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

        # 说明：不再注入「## 工具说明书」段 —— 工具定义已由 function-calling 的 tools 参数
        # 完整提供（含每个参数的 JSON Schema），prompt 内再写一份纯属重复，且信息更少。
        # 经 A/B 实测（2026-09-11）移除后工具选择无退化，输入字符 -29.6%。
        # generate_tool_manual() 保留在 toolkit.py，作为「provider 不支持 function calling」时的文本兜底。
        return '\n\n'.join(parts)

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
        renderers: dict[str, Callable[[dict[str, Any]], tuple[UIType, dict[str, Any]]]] = {
            'generate_diy_plan': lambda r: (UIType.PLAN_CARD, {'plans': [r]}),
            'revise_diy_plan': lambda r: (UIType.PLAN_CARD, {'plans': [r]}),
            'generate_effect_image': lambda r: (UIType.IMAGE_TASK, {'task_id': r.get('task_id'), 'poll': r.get('poll'), **({'result_url': r['result_url']} if r.get('result_url') else {})}),
            'create_order': lambda r: (UIType.ORDER_CARD, r),
            'render_greeting_card': lambda r: (UIType.GREETING_CARD, r),
        }
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
                if not isinstance(result, dict) or result.get('ok') is not True:
                    continue
                rows = result.get('data')
                if not isinstance(rows, list) or not rows:
                    continue
                entity = (tc.arguments or {}).get('entity')
                if entity == 'plan':
                    return (UIType.PLAN_CARD, {'plans': rows})
                if entity == 'shop':
                    return (UIType.SHOP_CARD, {'shops': rows})
                continue
            render = renderers.get(tc.name)
            if not render:
                continue
            if isinstance(result, list) and (not result):
                continue
            return render(result)
        return (UIType.TEXT, {})

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
