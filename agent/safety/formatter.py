"""Deterministic output privacy and rendering-safety formatting."""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("agent")


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


