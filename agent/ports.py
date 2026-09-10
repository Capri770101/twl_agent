"""端口层：只定义契约，不绑定具体实现。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol


class SessionStore(Protocol):
    async def get_or_create_session(self, user_id: str, conversation_id: str | None = None, shop_id: str | None = None, entry: str | None = None, product_id: str | None = None, product_title: str | None = None) -> str: ...
    async def create_conversation(self, user_id: str, title: str = "新对话", shop_id: str | None = None, entry: str | None = None, product_id: str | None = None, product_title: str | None = None) -> str: ...
    async def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None: ...
    async def get_stage(self, session_id: str) -> str: ...
    async def update_stage(self, session_id: str, stage: str) -> None: ...
    async def get_session_shop_id(self, session_id: str) -> str | None: ...
    async def get_session_context(self, session_id: str) -> dict[str, Any]: ...
    async def get_requirement(self, session_id: str): ...
    async def set_requirement(self, session_id: str, req: Any) -> None: ...
    async def get_long_term(self, user_id: str) -> dict[str, Any]: ...
    async def set_long_term(self, user_id: str, key: str, value: Any) -> None: ...
    async def get_session_json(self, user_id: str, session_id: str, key: str): ...
    async def set_session_json(self, user_id: str, session_id: str, key: str, value: Any) -> None: ...
    async def set_session_flag(self, user_id: str, session_id: str, key: str, value: str) -> None: ...
    async def get_session_flag(self, user_id: str, session_id: str, key: str) -> str | None: ...
    async def clear_session_flags(self, user_id: str, session_id: str, prefix: str = "") -> None: ...
    async def load_history(self, conversation_id: str, limit: int) -> list[dict[str, Any]]: ...


# 本地商品/订单镜像已随 2026-09 架构重构移除：不再有 PlanRepository / ShopRepository
# 等本地仓储契约。商品/店铺/订单一律通过平台只读查询
# （backend.data_gateway.external.query_external_entity，须有 active 映射）与平台自有
# 下单 API（backend 侧 create_order 适配器）访问，契约见 backend/data_gateway/。


class LLMProvider(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, stream: bool = False, response_format: dict[str, Any] | None = None, user_id: str | None = None) -> Any: ...
    def chat_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, user_id: str | None = None) -> Any: ...


class ImageProvider(Protocol):
    async def generate(self, prompt: str) -> str: ...


class KnowledgeBase(Protocol):
    def query(self, domain: str, query: str) -> list[dict[str, Any]]: ...
    def get_by_id(self, item_id: str) -> dict[str, Any] | None: ...


ToolHandler = Callable[[dict[str, Any] | None, dict[str, Any] | None], Awaitable[tuple[str, str]]]


# 前端「非店铺入口」传来的占位 shop_id：一律视为「未锁店」。
# 曾出现首页进入时前端传 shop_id='default'，被当成真实店铺写进 sessions、
# 拼进「店铺锁定模式」prompt、并用于 SQL 过滤 → 查什么都查不到。
_PLACEHOLDER_SHOP_IDS = frozenset({'', 'default', 'none', 'null', 'undefined', 'nil', 'n/a', 'na', '0', '-'})


def normalize_shop_id(shop_id: str | None) -> str | None:
    """把占位 shop_id（default/none/undefined/0/空等）规范成 None（未锁店）。

    Args:
        shop_id: 前端 / 宿主平台传入的原始 shop_id，可为 None。

    Returns:
        规范化后的 shop_id；占位值或空值返回 None（表示未锁定店铺）。
    """
    raw = str(shop_id or '').strip()
    if raw.lower() in _PLACEHOLDER_SHOP_IDS:
        return None
    return raw


# ── 会话入口（entry）：决定智能体是「店铺锁定」还是「全平台」模式 ──
# 业务语义（2026-09-10 定）：
#   product = 从商品详情页进入（该商品有店铺归属 → 锁店，且智能体知道用户在看哪个商品）
#   shop    = 从店铺详情页进入（锁店）
#   home    = 未选定任何商品/店铺（首页/搜索/分类等 → 全平台模式，可跨店推荐）
_ENTRY_ALIASES: dict[str, str] = {
    'product': 'product', 'goods': 'product', 'item': 'product', 'plan': 'product',
    'detail': 'product', 'product_detail': 'product', 'goodsdetail': 'product', 'sku': 'product',
    '商品': 'product', '商品详情': 'product', '商品详情页': 'product', '商品页': 'product',
    'shop': 'shop', 'store': 'shop', 'merchant': 'shop', 'shop_detail': 'shop',
    '店铺': 'shop', '店铺详情': 'shop', '店铺页': 'shop', '商家': 'shop', '商家详情': 'shop',
    'home': 'home', 'index': 'home', 'main': 'home', 'discover': 'home', 'search': 'home', 'category': 'home',
    '首页': 'home', '搜索': 'home', '分类': 'home',
}

VALID_ENTRIES = ('product', 'shop', 'home')


def normalize_product_id(value: str | None) -> str | None:
    """把占位 product_id（default/none/0/空等）规范成 None。

    Returns:
        规范化后的商品 ID；占位值或空值返回 None。
    """
    raw = str(value or '').strip()
    if raw.lower() in _PLACEHOLDER_SHOP_IDS:
        return None
    return raw[:64]


def normalize_product_title(value: str | None) -> str | None:
    """规范化商品标题（去空白、截断），占位值返回 None。"""
    raw = str(value or '').strip()
    if raw.lower() in _PLACEHOLDER_SHOP_IDS:
        return None
    return raw[:80]


def normalize_entry(entry: str | None, shop_id: str | None = None, product_id: str | None = None) -> str:
    """规范化会话入口类型：product（商品详情页）/ shop（店铺页）/ home（未选定）。

    前端可显式传 entry；不传或传了不认识的值时，按上下文推断：
    有 product_id → product；仅有 shop_id → shop；都没有 → home。

    Args:
        entry: 前端传入的入口标识（大小写、中英文均可）。
        shop_id: 已规范化的店铺 ID（None 表示未锁店）。
        product_id: 已规范化的商品 ID（None 表示用户没在看具体商品）。

    Returns:
        'product' / 'shop' / 'home' 之一。
    """
    mapped = _ENTRY_ALIASES.get(str(entry or '').strip().lower())
    if mapped:
        # 入口与上下文冲突时以上下文为准：声称从商品页进入却没有商品/店铺，按 home 处理。
        if mapped == 'product' and not (product_id or shop_id):
            return 'home'
        if mapped == 'shop' and not shop_id:
            return 'home'
        return mapped
    if product_id:
        return 'product'
    if shop_id:
        return 'shop'
    return 'home'
