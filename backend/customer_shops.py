"""公开门店只读查询，不携带客户凭据。"""
import re
from urllib.parse import urlsplit
import httpx
from backend.config import settings
from backend.customer_service import failure


async def query_shops(shop_id: str = '', city: str = '', *, transport=None) -> dict:
    if not settings.CUSTOMER_SERVICE_ENABLED:
        return failure('UNSUPPORTED')
    base = settings.CUSTOMER_SERVICE_BASE_URL.rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return failure('UNSUPPORTED')
    if (shop_id and not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', shop_id)) or len(city) > 80:
        return failure('INVALID_ARGUMENT')
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, transport=transport) as client:
            r = await client.get(base + '/shops' + ('/' + shop_id if shop_id else ''), params={'city': city} if not shop_id else None)
        if r.status_code == 404:
            return failure('NOT_FOUND')
        if r.status_code != 200:
            return failure('UPSTREAM_UNAVAILABLE', r.status_code >= 500)
        body = r.json()
        if not isinstance(body, dict) or body.get('ok') is not True:
            return failure('INVALID_UPSTREAM_RESPONSE')
        data = body.get('data')
        rows = [data] if shop_id else data.get('items')
        if not isinstance(rows, list) or any(not isinstance(p, dict) or not p.get('id') for p in rows):
            return failure('INVALID_UPSTREAM_RESPONSE')
        if shop_id and str(rows[0]['id']) != shop_id:
            return failure('INVALID_UPSTREAM_RESPONSE')
        fields = ('id', 'name', 'city', 'address', 'business_hours', 'delivery_policy', 'service_phone', 'updated_at')
        return {'ok': True, 'data': {'shops': [{k: p.get(k) for k in fields} for p in rows[:20]]},
                'fetched_at': body.get('fetched_at'), 'source': 'shop_backend'}
    except httpx.TimeoutException:
        return failure('UPSTREAM_TIMEOUT', True)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return failure('UPSTREAM_UNAVAILABLE', True)
