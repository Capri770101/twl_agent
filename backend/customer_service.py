"""客户业务只读适配器。尚未注册模型工具；凭据必须来自请求执行上下文。"""
from urllib.parse import urlsplit
import re
import httpx
from backend.config import settings


def failure(code: str, retryable: bool = False) -> dict:
    return {'ok': False, 'code': code, 'retryable': retryable}


async def query_orders(credential: str, *, order_id: str | None = None, page: int = 1,
                       after_sales: bool = False, transport=None) -> dict:
    if not settings.CUSTOMER_SERVICE_ENABLED:
        return failure('UNSUPPORTED')
    if not isinstance(credential, str) or not credential.startswith('cs1.') or len(credential) > 2048 or '\r' in credential or '\n' in credential:
        return failure('AUTH_REQUIRED')
    base = settings.CUSTOMER_SERVICE_BASE_URL.rstrip('/')
    url = urlsplit(base)
    # 票据属于敏感凭据，只发到运维配置的可信 HTTPS 源，不跟随重定向。
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        return failure('UNSUPPORTED')
    if order_id is not None and (not isinstance(order_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', order_id)):
        return failure('INVALID_ARGUMENT')
    if type(page) is not int or not 1 <= page <= 10000:
        return failure('INVALID_ARGUMENT')
    path = '/orders/' + order_id if order_id is not None else '/orders'
    if after_sales:
        if not order_id:
            return failure('INVALID_ARGUMENT')
        path += '/after-sales'
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, transport=transport) as client:
            response = await client.get(base + path, params={'page': page} if order_id is None else None,
                                        headers={'Authorization': 'Bearer ' + credential})
        if response.status_code == 401:
            return failure('AUTH_REQUIRED')
        if response.status_code == 403:
            return failure('FORBIDDEN')
        if response.status_code == 404:
            return failure('NOT_FOUND')
        if response.status_code == 429:
            return failure('RATE_LIMITED', True)
        if response.status_code != 200:
            return failure('UPSTREAM_UNAVAILABLE', response.status_code >= 500)
        body = response.json()
        if not isinstance(body, dict) or body.get('ok') is not True or not isinstance(body.get('data'), dict):
            return failure('INVALID_UPSTREAM_RESPONSE')
        # 不透传未约定的字段或上游错误文本给模型。
        allowed = ('id', 'status', 'shop_name', 'total_price_cents', 'created_at', 'expected_delivery')
        def clean(row):
            if not isinstance(row, dict) or not row.get('id'):
                raise ValueError('invalid order')
            return {k: row[k] for k in allowed if k in row}
        data = body['data']
        if order_id is None:
            if not isinstance(data.get('items'), list) or type(data.get('has_more')) is not bool:
                return failure('INVALID_UPSTREAM_RESPONSE')
            result = {'items': [clean(row) for row in data['items'][:10]], 'has_more': data['has_more'], 'page': page}
        else:
            if str(data.get('id')) != order_id:
                return failure('INVALID_UPSTREAM_RESPONSE')
            result = clean(data)
            if after_sales:
                details = data.get('after_sales')
                if not isinstance(details, dict) or details.get('state') not in {'pending_review', 'processing', 'succeeded', 'failed', 'rejected', 'none', 'unknown'}:
                    return failure('INVALID_UPSTREAM_RESPONSE')
                result['after_sales'] = {k: details.get(k) for k in ('state', 'merchant_review', 'amount_cents', 'applied_at', 'refunded_at')}
        return {'ok': True, 'data': result, 'source': 'h5_orders', 'fetched_at': body.get('fetched_at')}
    except httpx.TimeoutException:
        return failure('UPSTREAM_TIMEOUT', True)
    except httpx.HTTPError:
        return failure('UPSTREAM_UNAVAILABLE', True)
    except (ValueError, TypeError):
        return failure('INVALID_UPSTREAM_RESPONSE')
