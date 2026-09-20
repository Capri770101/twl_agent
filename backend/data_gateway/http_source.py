"""只读 HTTP 数据源：把平台 REST API 当作外部数据源（替代「直连只读库」）。

为什么需要它
------------
原架构要求平台方提供**只读数据库连接串**（``PLATFORM_DB_<SOURCE_ID>_URL``）并配置
active mapping，否则 ``query_external_entity`` 直接拒答——这是长期卡住「查商品 / 推店铺」
的硬阻断。平台已提供等价能力的 REST 接口，本模块把它接成**第二种只读来源**：

    环境变量：PLATFORM_API_<SOURCE_ID>_URL = https://aistore.xiangbinmeigui.com
    可选：    PLATFORM_API_<SOURCE_ID>_TOKEN = <Bearer token>（仅在需要鉴权的端点上用）

优先级：**DB 数据源优先**。只有该 source 未配 ``PLATFORM_DB_*_URL``（或未配 active mapping）
时才落到 HTTP，保证既有部署行为零变化。

实体映射（只读，且只暴露业务必需字段）
--------------------------------------
- ``plan``  → ``GET /v1/merchant/products``（在售商品/方案）
- ``shop``  → ``GET /v1/merchant/shops``（店铺）
- ``order`` / ``user`` → **不支持**（平台未开放匿名读取，需登录态；本智能体不处理订单）

字段规整约定（与既有映射体系保持一致）
--------------------------------------
1. 输出 canonical 名沿用平台**库列名风格**（snake_case），金额字段由「分」转「元」——
   与 ``data_mapping`` 里 ``cents_to_yuan`` 的约定一致，下游展示层（商品卡片等）无需改动；
2. **敏感字段一律丢弃**（证件照 / 营业执照 / 微信支付商户号 / 分账比例等）：
   这些字段平台当前是匿名可读的，我们更不该把它们带进对话上下文与卡片数据；
3. 过滤在本地做（keyword / shop_id / id）——列表规模（约 1.9k 商品 / 12 店铺）足够小，
   且避免依赖平台侧未公开的查询参数。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any

from backend.data_gateway.external import _annotate_open_status

logger = logging.getLogger('data_gateway.http_source')

_PREFIX = 'PLATFORM_API_'
_SUFFIX_URL = '_URL'
_SUFFIX_TOKEN = '_TOKEN'
_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

MAX_LIMIT = 100
TIMEOUT = 12.0

# ── 进程内 TTL 缓存 ────────────────────────────────────────────────────────
# 为什么需要：平台列表接口**不支持查询参数**（关键词/店铺过滤都在本地做），
# 每次 fetch 都是「拉全量 + 本地筛」。一轮咨询里模型常查 2~4 次（不同关键词或实体），
# 而平台数据秒级内不会变 —— 实测单次拉取 0.3~2s，重复拉纯属浪费。
# 键含 token 指纹：不同数据源凭据不同，串了会串数据。
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def cache_ttl() -> float:
    """HTTP 拉取的缓存有效期（秒）。<=0 表示关闭缓存。

    Returns:
        环境变量 ``PLATFORM_HTTP_CACHE_TTL`` 的浮点值，缺省 60 秒。
    """
    try:
        return float(os.getenv('PLATFORM_HTTP_CACHE_TTL', '60') or '60')
    except ValueError:
        return 60.0


def clear_cache() -> None:
    """清空 HTTP 缓存（手动刷新 / 测试用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()

# 实体 → 平台资源路径（只读列表接口）
ENTITY_RESOURCES: dict[str, str] = {
    'plan': '/v1/merchant/products',
    'shop': '/v1/merchant/shops',
}
UNSUPPORTED_ENTITIES = {'order', 'user'}

# 平台字段（camelCase）→ 规范字段（snake_case）。未列出的字段**不输出**（白名单式）。
PLAN_FIELDS: dict[str, str] = {
    'id': 'id',
    'name': 'name',
    'price': 'price',
    'originalPrice': 'original_price',
    'subtitle': 'subtitle',
    'description': 'description',
    'flowerMeaning': 'flower_meaning',
    'flowers': 'flowers',
    'image': 'image',
    'images': 'images',
    'tags': 'tags',
    'rating': 'rating',
    'sales': 'sales',
    'stock': 'stock',
    'season': 'season',
    'shelfLife': 'shelf_life',
    'categoryId': 'category_id',
    'ownerShopId': 'shop_id',
    'status': 'status',
    'createTime': 'created_at',
    'updateTime': 'updated_at',
}

SHOP_FIELDS: dict[str, str] = {
    'id': 'id',
    'name': 'name',
    'city': 'city',
    'address': 'address',
    'phone': 'phone',
    'avatar': 'avatar',
    'cover': 'cover',
    'rating': 'rating',
    'ratingCount': 'rating_count',
    'status': 'status',
    'businessHours': 'business_hours',
    'tags': 'tags',
    'categories': 'categories',
    'description': 'description',
    'brandSlogan': 'brand_slogan',
    'ipTitle': 'ip_title',
    'ipText': 'ip_text',
    'serviceNote': 'service_note',
    'deliveryFee': 'delivery_fee',
    'deliveryTime': 'delivery_time',
    'minOrderPrice': 'min_order_price',
    'monthSales': 'month_sales',
    'flowers': 'flowers',
    'featuredFlowerIds': 'featured_flower_ids',
    'doorPhoto': 'door_photo',
    'envPhotos': 'env_photos',
    'distance': 'distance',
}

_FIELDS_BY_ENTITY: dict[str, dict[str, str]] = {'plan': PLAN_FIELDS, 'shop': SHOP_FIELDS}

# 以「分」为单位的金额字段（需 ÷100）；与 data_mapping 的 cents_to_yuan 语义一致
_AMOUNT_FIELDS: dict[str, set[str]] = {
    'plan': {'price', 'original_price'},
    'shop': {'delivery_fee', 'min_order_price'},
}

# 显式丢弃的敏感字段（白名单已经排除了它们，这里再声明一次作为「别加回来」的护栏）
SENSITIVE_FIELDS: frozenset[str] = frozenset({
    'subMchId', 'profitSharingRatio', 'idFront', 'idBack', 'license',
    'wechatQr', 'wechatQrCode', 'qrcodeUrl', 'sharePath',
})


# ── 配置读取 ────────────────────────────────────────────────────────────────

def source_ids() -> list[str]:
    """扫描环境变量，返回已配置的 HTTP 数据源 source_id（小写、去重、排序）。"""
    ids: set[str] = set()
    for key in os.environ:
        if key.startswith(_PREFIX) and key.endswith(_SUFFIX_URL):
            mid = key[len(_PREFIX):-len(_SUFFIX_URL)]
            if mid:
                ids.add(mid.lower())
    return sorted(ids)


def api_url(source_id: str) -> str:
    """取该 source 的 API 基地址；未配置或非法时返回空串。"""
    if not _IDENTIFIER.match(source_id or ''):
        return ''
    return os.getenv(f'{_PREFIX}{source_id.upper()}{_SUFFIX_URL}', '').strip()


def api_token(source_id: str) -> str:
    """取可选 Bearer token（需要鉴权的端点才用得上）。"""
    if not _IDENTIFIER.match(source_id or ''):
        return ''
    return os.getenv(f'{_PREFIX}{source_id.upper()}{_SUFFIX_TOKEN}', '').strip()


def is_configured(source_id: str) -> bool:
    """该 source 是否配了 HTTP 数据源（配了才走本模块）。"""
    url = api_url(source_id)
    return bool(url) and url.lower().startswith(('http://', 'https://'))


# ── 取数与规整 ──────────────────────────────────────────────────────────────

def _fetch_json(url: str, token: str = '', timeout: float = TIMEOUT) -> dict[str, Any]:
    """GET 一个 JSON 接口（失败抛异常，由调用方兜底）。

    Args:
        url: 完整 URL。
        token: 可选 Bearer token。
        timeout: 超时秒数。

    Returns:
        解析后的 JSON dict（可能是共享的缓存对象，调用方**不得原地修改**）。

    Raises:
        RuntimeError: 网络/HTTP/JSON 解析失败。
    """
    ttl = cache_ttl()
    cache_key = ''
    if ttl > 0:
        cache_key = hashlib.sha256(f'{url}\x00{token}'.encode()).hexdigest()[:20]
        with _CACHE_LOCK:
            hit = _CACHE.get(cache_key)
        if hit and time.time() - hit[0] < ttl:
            logger.debug('[http_source] 缓存命中 %s', url)
            return hit[1]

    import httpx

    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f'请求平台接口失败：{type(exc).__name__}') from exc
    if resp.status_code != 200:
        raise RuntimeError(f'平台接口返回 HTTP {resp.status_code}')
    text = resp.content.decode('utf-8', 'replace')
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # 平台部分错误信息以 GBK 编码，容错再试一次
        try:
            payload = json.loads(resp.content.decode('gbk', 'replace'))
        except json.JSONDecodeError as exc:
            raise RuntimeError('平台接口返回的不是合法 JSON') from exc
    if cache_key:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (time.time(), payload)
    return payload


def _unwrap(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """解开 ``{code, data:{list:[...]}}`` 信封；业务码非 0 视为失败。

    注意：平台**HTTP 状态码恒为 200**，鉴权与业务失败写在 body 的 ``code`` 里
    （如 ``{"code":401,"message":"请先登录"}``），不能按状态码判断。
    """
    if not isinstance(payload, dict):
        raise RuntimeError('平台接口返回结构异常')
    code = payload.get('code')
    if code not in (0, '0', None):
        message = str(payload.get('message') or '').strip() or f'code={code}'
        raise RuntimeError(f'平台接口拒绝：{message}')
    data = payload.get('data')
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        rows = data.get('list')
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        return [data]
    return []


def _cents_to_yuan(value: Any) -> Any:
    """分 → 元（保留 2 位；非数值原样返回）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return round(float(value) / 100, 2)


def normalize_row(entity: str, raw: dict[str, Any]) -> dict[str, Any]:
    """平台一行 → 规范一行（白名单取字段 + 金额换算）。"""
    fields = _FIELDS_BY_ENTITY.get(entity) or {}
    amounts = _AMOUNT_FIELDS.get(entity) or set()
    out: dict[str, Any] = {}
    for src, dst in fields.items():
        if src in SENSITIVE_FIELDS or src not in raw:
            continue
        value = raw.get(src)
        if dst in amounts:
            value = _cents_to_yuan(value)
        out[dst] = value
    return out


def _match_scope(row: dict[str, Any], entity: str, shop_id: str, row_id: str) -> bool:
    """硬约束过滤：主键 / 店铺。**不参与关键词放宽**。

    Args:
        row: 已规范化的行。
        entity: 实体名（决定店铺列的 canonical 字段名）。
        shop_id: 限定店铺（空 = 不限定）。
        row_id: 限定主键（空 = 不限定）。

    Returns:
        是否通过硬约束。
    """
    if row_id and str(row.get('id') or '') != str(row_id):
        return False
    if shop_id:
        shop_col = 'shop_id' if entity == 'plan' else 'id'
        if str(row.get(shop_col) or '') != str(shop_id):
            return False
    return True


_KEYWORD_SEP = re.compile(r'[\s,，、;；+/|·]+')


def _split_keywords(keyword: str) -> list[str]:
    """把关键词串拆成独立词元（去空、去重、保序、小写）。

    为什么需要拆：模型常把多个词堆在一起（``"妈妈 康乃馨"``），而旧实现把整串当
    一个 needle 做子串匹配 —— 商品名里不可能同时出现这串字面，**必然返回空**，
    模型于是换关键词再试一轮（实测白烧一次 6.5s 的 LLM 往返）。

    Args:
        keyword: 原始关键词，可能含空格/顿号/逗号等分隔符。

    Returns:
        小写词元列表。
    """
    seen: set[str] = set()
    out: list[str] = []
    for part in _KEYWORD_SEP.split((keyword or '').strip()):
        token = part.strip().lower()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _keyword_hits(row: dict[str, Any], tokens: list[str]) -> int:
    """统计行命中几个关键词元（0 = 完全不命中）。

    Args:
        row: 已规范化的行。
        tokens: :func:`_split_keywords` 拆出的词元。

    Returns:
        命中的词元个数。
    """
    if not tokens:
        return 0
    haystack = ' '.join(
        str(row.get(k) or '') for k in ('name', 'subtitle', 'description', 'flower_meaning', 'tags', 'city')
    ).lower()
    return sum(1 for t in tokens if t in haystack)


# 常见颜色字（用于识别「白绿」这类连写色词并拆单字）
_COLOR_CHARS = frozenset('红橙黄绿青蓝紫粉白黑灰棕褐金银')


def _token_levels(tokens: list[str]) -> list[list[str]]:
    """把词元按「字面放宽层级」展开：原形 → 去「系」→ 去「色」→ 色词拆单字。

    Args:
        tokens: :func:`_split_keywords` 拆出的词元。

    Returns:
        逐级放宽的词元列表（首项是原形；无变化时不重复追加）。

    为什么需要（2026-09-18 线上实测）：中文色彩词的**使用形式不统一** ——
    用户/模型说「粉色系」「香槟色」，而商品文案里写「粉色」「香槟」。整词匹配会**零命中**，
    于是放宽成「全量」，表现出来就是审计说的**「色系检索不过滤」**
    （查「粉色系」拿到「随机花瓶一个」这类无关商品）。逐级去后缀能精准得多：
    `粉色系 → 粉色 → 粉`、`香槟色 → 香槟`、`白绿色系 → 白绿色 → 白绿 → 白 + 绿`。
    """
    levels = [list(tokens)]
    lv = [t[:-1] if t.endswith('系') and len(t) > 2 else t for t in levels[-1]]
    if lv != levels[-1]:
        levels.append(lv)
    lv = [t[:-1] if t.endswith('色') and len(t) > 1 else t for t in levels[-1]]
    if lv != levels[-1]:
        levels.append(lv)
    # 末级：纯颜色字组成的词拆成单字（「白绿」→「白」「绿」）—— 商品文案常把两种颜色
    # **分开写**（「白色百合配绿叶」），连写形式会零命中。OR 匹配下任一命中即算，
    # 再按命中数排序，精度损失可接受。
    split: list[str] = []
    for t in levels[-1]:
        if len(t) >= 2 and all(ch in _COLOR_CHARS for ch in t):
            split.extend(t)
        else:
            split.append(t)
    split = list(dict.fromkeys(split))
    if split != levels[-1]:
        levels.append(split)
    return levels


def fetch_entity(
    source_id: str,
    entity: str,
    keyword: str = '',
    limit: int = 100,
    shop_id: str = '',
    row_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """只读查询标准业务实体（HTTP 通路）。

    Args:
        source_id: 数据源 ID（对应 ``PLATFORM_API_<SOURCE_ID>_URL``）。
        entity: ``plan`` / ``shop``；``order`` / ``user`` 平台未开放，会抛错。
        keyword: 关键词（在名称/副标题/描述/花语/标签/城市里做子串匹配）。
        limit: 返回条数上限（1–100）。
        shop_id: 只看该店铺（plan 按所属店铺，shop 按自身 id）。
        row_id: 按主键精确查单行（商品详情页进入时用）。

    Returns:
        规范化的行列表（字段见模块文档；空结果返回空列表）。

    Raises:
        ValueError: entity 非法或不支持。
        RuntimeError: 未配置 / 请求失败 / 平台业务码非 0。
    """
    from backend.data_gateway.access import require_source
    require_source(source_id)
    if not _IDENTIFIER.match(entity or ''):
        raise ValueError('invalid entity')
    resource = ENTITY_RESOURCES.get(entity)
    if not resource:
        if entity in UNSUPPORTED_ENTITIES:
            raise RuntimeError(f'该数据源未开放 {entity} 实体（平台需登录态，智能体不处理订单）')
        raise ValueError(f'unsupported entity: {entity}')

    base = api_url(source_id)
    if not base:
        raise RuntimeError('未配置 HTTP 数据源')
    payload = _fetch_json(base.rstrip('/') + resource, api_token(source_id))
    rows = _unwrap(payload)

    out: list[dict[str, Any]] = []
    cap = max(1, min(int(limit or MAX_LIMIT), MAX_LIMIT))
    tokens = _split_keywords(keyword)
    mode = 'all'

    if tokens:
        # 逐级放宽字面：原形 → 去「系」→ 去「色」。第一级命中就不用更松的（保精准）。
        for level, level_tokens in enumerate(_token_levels(tokens)):
            scored: list[tuple[int, dict[str, Any]]] = []
            for raw in rows:
                row = normalize_row(entity, raw)
                if not _match_scope(row, entity, shop_id, row_id or ''):
                    continue
                hits = _keyword_hits(row, level_tokens)
                if hits:
                    scored.append((hits, row))
            if scored:
                # 命中词元多的排前面：「妈妈 康乃馨」里命中「康乃馨」的比只命中「妈妈」的更相关
                scored.sort(key=lambda item: -item[0])
                mode = 'exact' if (level == 0 and scored[0][0] == len(tokens)) else 'partial'
                out = [row for _, row in scored[:cap]]
                break

    if not out:
        # 无关键词，或关键词零命中 → 扫全量（仍守 shop_id / row_id 硬约束）。
        # ⚠️ 零命中时**刻意不返回空**：返空会让模型换个关键词再试一轮（实测白烧一次
        # 6.5s 的 LLM 往返），而它最终需要的还是这批数据。改为如实标注 mode='relaxed'，
        # 由上层告知模型「未按该关键词筛出、以下是全部在售」，让模型自己判断怎么用。
        if tokens:
            mode = 'relaxed'
        for raw in rows:
            row = normalize_row(entity, raw)
            if not _match_scope(row, entity, shop_id, row_id or ''):
                continue
            out.append(row)
            if len(out) >= cap:
                break

    # 营业状态按「此刻」实时推算（与 DB 通路同一套派生字段，不写平台）
    _annotate_open_status(out)
    if meta is not None:
        meta.update({
            'match': mode,
            'keyword_tokens': tokens,
            'matched': len(out),
            'scanned': len(rows),
        })
    logger.info('[http_source] source=%s entity=%s match=%s 返回=%d/%d 词元=%s',
                source_id, entity, mode, len(out), len(rows), tokens)
    return out
