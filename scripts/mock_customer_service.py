#!/usr/bin/env python3
"""本地 mock H5 业务后端 —— 离线联调客户服务链路用。

为什么需要它
------------
老板已封生产环境（2026-10-08 起禁止登录 H5 服务器），agent 侧开发不能依赖真实
H5。本脚本按 ``docs/customer-service-contract.md``（v1.4.0 冻结契约）实现全部
三个接口 + cs1 票据签发/验证，跑在本地，供：

* ``tests/test_customer_*`` 之外的端到端调试（``APP_ENV=dev`` + HTTP 放行）；
* 故障注入演练（超时 / 5xx / 401 / 404 / 限流），验证 agent 侧降级文案；
* fixture 数据覆盖全订单状态机（10 个状态 × 售后 7 态）。

用法::

    # 终端 1（或 python -m scripts.mock_customer_service）
    CUSTOMER_SERVICE_SECRET=dev_secret_at_least_32_bytes_xxx \
        python scripts/mock_customer_service.py --port 4761

    # 终端 2：agent 侧指向 mock（.env）
    CUSTOMER_SERVICE_ENABLED=true
    CUSTOMER_SERVICE_BASE_URL=http://127.0.0.1:4761/api/customer-service
    CUSTOMER_SERVICE_SECRET=<同上>
    CUSTOMER_SERVICE_ALLOW_INSECURE=true   # 仅 dev；见 customer_service.py 门控
    APP_ENV=dev

与生产契约的一致性由 ``tests/test_customer_credential_interop.py``
（H5 JS 实现的 Python 互操作测试）共同保证：本 mock 的票据实现复用
``backend/customer_identity.py`` 的同一套 HMAC 逻辑。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHOPS = [
    {'id': 's001', 'name': '千百度花坊', 'city': '舟山', 'address': '浙江省舟山市定海区东海东路122号',
     'business_hours': '07:00-22:00', 'delivery_policy': None, 'service_phone': '13567675068', 'updated_at': None},
    {'id': 's007', 'name': '花诺花艺', 'city': '福州', 'address': '福建省福州市台江区上海街道西环中路674-6号',
     'business_hours': '08:00-22:00', 'delivery_policy': None, 'service_phone': '18650717901', 'updated_at': None},
    {'id': 's015', 'name': '花美家AI花店（华农店）', 'city': '广州市', 'address': '广州市天河区五山街道大丰路自编18号B栋',
     'business_hours': '08:00-22:00', 'delivery_policy': '同城2小时达，满99免运费', 'service_phone': '13923439966', 'updated_at': None},
]

# 全状态机订单：user 1001 名下 10 笔，覆盖 §5 状态表全部状态 + 售后各态。
_ORDERS_SPEC = [
    # (id, status, cents, shop, after_sales_state, merchant_review)
    ('MOCK2026100101', 'pending', 12800, '花诺花艺', None, None),
    ('MOCK2026100102', 'paid', 25600, '千百度花坊', None, None),
    ('MOCK2026100103', 'making', 23600, '花美家AI花店（华农店）', None, None),
    ('MOCK2026100104', 'delivering', 9900, '花诺花艺', None, None),
    ('MOCK2026100105', 'completed', 18800, '千百度花坊', None, None),
    ('MOCK2026100106', 'cancelled', 100, '花诺花艺', None, None),
    ('MOCK2026100107', 'refund_applying', 15900, '花诺花艺', 'pending_review', None),
    ('MOCK2026100108', 'refunding', 20700, '千百度花坊', 'processing', 'approved'),
    ('MOCK2026100109', 'refunded', 13600, '花美家AI花店（华农店）', 'succeeded', 'approved'),
    ('MOCK2026100110', 'refund_failed', 8000, '花诺花艺', 'failed', 'rejected'),
    ('MOCK2026100111', 'refund_applying', 5000, '千百度花坊', 'rejected', 'rejected'),
]
T0 = '2026-10-01 10:0'
ORDERS: dict[str, dict] = {}
for i, (oid, status, cents, shop, as_state, audit) in enumerate(_ORDERS_SPEC):
    ORDERS[oid] = {'id': oid, 'status': status, 'shop_name': shop, 'total_price_cents': cents,
                   'created_at': f'{T0}{i}:00', 'expected_delivery': '2026-10-02 18:00',
                   '_after_state': as_state, '_audit': audit}
UID = 1001

class Handler(BaseHTTPRequestHandler):
    secret = b''
    fault = ''          # 'timeout' | '500' | '401' | '404' | '429'，由 --fault 指定
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # 静音默认日志，保留自定义一行
        print(f'[mock] {self.command} {self.path}', flush=True)

    # ── 工具 ──
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, code: str, retryable: bool = False) -> None:
        self._json(status, {'ok': False, 'code': code, 'trace_id': str(uuid.uuid4()),
                            'retryable': retryable or status == 503})

    def _issue(self, uid: int) -> str:
        payload = {'uid': uid, 'aud': 'twl-customer-service', 'scope': 'orders:read',
                   'iat': int(time.time()), 'exp': int(time.time()) + 300, 'jti': str(uuid.uuid4())}
        encoded = _b64(json.dumps(payload).encode())
        sig = _b64(hmac.new(self.secret, f'cs1.{encoded}'.encode(), hashlib.sha256).digest())
        return f'cs1.{encoded}.{sig}'

    def _verify(self, token: str) -> int | None:
        parts = token.split('.')
        if len(parts) != 3 or parts[0] != 'cs1':
            return None
        expected = hmac.new(self.secret, f'cs1.{parts[1]}'.encode(), hashlib.sha256).digest()
        import base64
        try:
            actual = base64.urlsafe_b64decode(parts[2] + '=' * (-len(parts[2]) % 4))
        except Exception:
            return None
        if not hmac.compare_digest(actual, expected):
            return None
        try:
            p = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        except Exception:
            return None
        now = int(time.time())
        if (p.get('aud') != 'twl-customer-service' or p.get('scope') != 'orders:read'
                or p.get('exp', 0) - p.get('iat', 0) != 300 or p.get('iat', 0) > now
                or p.get('exp', 0) <= now or not isinstance(p.get('uid'), int)):
            return None
        return p['uid']

    def _order_public(self, row: dict) -> dict:
        return {'id': row['id'], 'status': row['status'], 'shop_name': row['shop_name'],
                'total_price_cents': row['total_price_cents'], 'created_at': row['created_at'],
                'expected_delivery': row['expected_delivery']}

    # ── 路由 ──
    def do_GET(self) -> None:
        if self.path.startswith('/api/customer-service/health'):
            self._json(200, {'ok': True, 'mock': True})
            return
        if not self.path.startswith('/api/customer-service'):
            self._fail(404, 'NOT_FOUND')
            return
        if self.fault:
            if self.fault == 'timeout':
                time.sleep(30)             # 超过 agent 侧 10s 工具超时
            elif self.fault == '500':
                self._fail(503, 'UPSTREAM_UNAVAILABLE', True)
                return
            elif self.fault == '401':
                self._fail(401, 'AUTH_REQUIRED')
                return
            elif self.fault == '429':
                self._fail(429, 'RATE_LIMITED', True)
                return
        rest = self.path[len('/api/customer-service'):]
        if rest == '/shops' or rest.startswith('/shops?'):
            city = ''
            if '?' in rest:
                from urllib.parse import parse_qs, urlsplit
                qs = parse_qs(urlsplit(self.path).query)
                city = (qs.get('city') or [''])[0]
            items = [dict(s) for s in SHOPS if not city or (s['city'] or '') == city]
            self._json(200, {'ok': True, 'data': {'items': items, 'has_more': False},
                             'source': 'shop_backend', 'fetched_at': _now_iso(), 'trace_id': str(uuid.uuid4())})
            return
        if rest.startswith('/shops/'):
            sid = rest[len('/shops/'):].split('?')[0]
            row = next((s for s in SHOPS if s['id'] == sid), None)
            if not row:
                self._fail(404, 'NOT_FOUND')
                return
            self._json(200, {'ok': True, 'data': dict(row), 'source': 'shop_backend',
                             'fetched_at': _now_iso(), 'trace_id': str(uuid.uuid4())})
            return
        # 以下接口需要票据
        token = (self.headers.get('Authorization') or '')
        if not token.startswith('Bearer cs1.'):
            self._fail(401, 'AUTH_REQUIRED')
            return
        uid = self._verify(token[7:])
        if uid is None:
            self._fail(401, 'AUTH_REQUIRED')
            return
        if uid != UID:
            self._fail(403, 'FORBIDDEN')
            return
        if rest == '/orders' or rest.startswith('/orders?'):
            page = 1
            if '?' in rest:
                from urllib.parse import parse_qs, urlsplit
                page = int((parse_qs(urlsplit(self.path).query).get('page') or ['1'])[0])
            rows = sorted(ORDERS.values(), key=lambda r: r['created_at'], reverse=True)
            chunk = rows[(page - 1) * 10: (page - 1) * 10 + 11]
            self._json(200, {'ok': True, 'data': {'items': [self._order_public(r) for r in chunk[:10]],
                                                  'page': page, 'has_more': len(chunk) > 10},
                             'source': 'h5_orders', 'fetched_at': _now_iso(), 'trace_id': str(uuid.uuid4())})
            return
        if rest.startswith('/orders/') and rest.endswith('/after-sales'):
            oid = rest[len('/orders/'):-len('/after-sales')]
            row = ORDERS.get(oid)
            if not row:
                self._fail(404, 'NOT_FOUND')
                return
            data = self._order_public(row)
            data['after_sales'] = {'state': row['_after_state'] or 'none', 'merchant_review': row['_audit'] or 'unknown',
                                   'amount_cents': row['total_price_cents'] if row['_after_state'] else None,
                                   'applied_at': row['created_at'] if row['_after_state'] else None,
                                   'refunded_at': row['created_at'] if row['_after_state'] == 'succeeded' else None}
            self._json(200, {'ok': True, 'data': data, 'source': 'h5_orders',
                             'fetched_at': _now_iso(), 'trace_id': str(uuid.uuid4())})
            return
        if rest.startswith('/orders/'):
            oid = rest[len('/orders/'):].split('?')[0]
            row = ORDERS.get(oid)
            if not row:
                self._fail(404, 'NOT_FOUND')
                return
            self._json(200, {'ok': True, 'data': self._order_public(row), 'source': 'h5_orders',
                             'fetched_at': _now_iso(), 'trace_id': str(uuid.uuid4())})
            return
        self._fail(404, 'NOT_FOUND')

    def do_POST(self) -> None:
        if self.path == '/api/customer-service/credential':
            # 契约 §6.2：换票只接受真实登录凭据；mock 里固定签给 UID。
            body = {'ok': True, 'data': {'access_token': self._issue(UID), 'token_type': 'Bearer',
                                         'expires_in': 300, 'scope': 'orders:read'},
                    'trace_id': str(uuid.uuid4())}
            self._json(200, body)
            return
        self._fail(404, 'NOT_FOUND')


def _b64(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=4761)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--secret', default='dev_secret_at_least_32_bytes_padding!!')
    ap.add_argument('--fault', choices=['', 'timeout', '500', '401', '404', '429'], default='',
                    help='故障注入：让所有需鉴权接口返回该故障（默认关闭）')
    args = ap.parse_args()
    Handler.secret = args.secret.encode()
    Handler.fault = args.fault
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'[mock] H5 业务后端 mock 监听 http://{args.host}:{args.port}'
          f'/api/customer-service  (fault={args.fault or "off"}, uid={UID})', flush=True)
    print(f'[mock] 换票: curl -X POST http://{args.host}:{args.port}/api/customer-service/credential', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
