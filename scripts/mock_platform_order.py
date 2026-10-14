#!/usr/bin/env python3
"""本地 mock 平台下单 API —— 离线联调 create_order 交易链路用。

为什么需要它
------------
`create_order`（agent/skills/skill_order.py）受 `PLATFORM_ORDER_ENABLED` +
`PLATFORM_ORDER_API_URL` 双条件门禁（1.5.0 起）。真实平台下单端点由平台方交付，
联调排期不受 agent 侧控制；本脚本按 skill_order 的部署契约实现该端点，供：

* ``tests/test_order_channel_e2e.py`` 离线端到端回归（payload 组装 → HTTP 提交 →
  响应解析 → order_card/pay_jump UI → trade 护栏凭真实工具证据放行「已下单」）；
* 手动联调演练与故障注入（HTTP 500 / 业务失败 / 缺订单号），验证 agent 侧
  「照实说明失败、绝不谎称成功」的降级口径。

契约（与 skill_order.py 文档字符串对齐）::

    POST {PLATFORM_ORDER_API_URL}
    → {"request_id": "AG...", "channel": "flora_agent", "external_user_id": "...",
       "agent_session_id": "...", "shop_id": "...", "plan": {...}, "items": [...],
       "estimated_total": 199.0, "remark": "..."}
    ← {"order_id": "PMOCK...", "status": "created", "total_price": 199.0,
       "pay": {"type": "miniapp", "page_path": "/pages/order/confirm",
               "params": {"order_id": "PMOCK...", "total_price": 199.0}}}

幂等：同一 ``request_id`` 重复提交返回同一订单（平台侧去重语义的最小实现）。

用法::

    python scripts/mock_platform_order.py --port 4762

    # agent 侧 .env（联调演练时）：
    PLATFORM_ORDER_ENABLED=true
    PLATFORM_ORDER_API_URL=http://127.0.0.1:4762/v1/orders
    # PLATFORM_ORDER_API_KEY=dev_order_key   # 可选；mock 只在配置后校验 Bearer 头

⚠️ 这是**离线契约验证**，不等于真实平台联调：字段名/金额单位（分 vs 元）/支付跳转
参数必须等平台 API 文档到位后在 `_build_payload` / `_parse_platform_order` 对齐，
并走真机验收（DELIVERY §5）。
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: 必填契约字段（缺失 → 400，模拟平台侧参数校验）。
_REQUIRED_FIELDS = ('request_id', 'shop_id', 'plan', 'items')


class Handler(BaseHTTPRequestHandler):
    """平台下单端点的最小实现 + 故障注入开关。

    类属性即注入面板（测试里直接改）：
    - ``mode``：'ok' | 'http500' | 'business_error' | 'no_order_id' | 'bad_json'
    - ``received``：收到的全部 payload（测试断言组装正确性）
    - ``orders``：request_id → 响应体（幂等去重）
    - ``require_auth``：True 时校验 Authorization: Bearer 头存在
    """

    mode = 'ok'
    received: list[dict] = []
    orders: dict[str, dict] = {}
    require_auth = False
    _lock = threading.Lock()
    _seq = 0

    # ── 故障注入面板复位（测试 fixture 用）──
    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls.mode = 'ok'
            cls.received = []
            cls.orders = {}
            cls.require_auth = False
            cls._seq = 0

    def log_message(self, fmt, *args):  # noqa: A003 - 静默访问日志
        pass

    def _send(self, code: int, body: dict | str) -> None:
        raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        data = raw.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - 健康检查
        if self.path.rstrip('/') in ('', '/health'):
            self._send(200, {'ok': True, 'service': 'mock_platform_order'})
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {'error': '请求体不是合法 JSON'})
            return
        if not isinstance(payload, dict):
            self._send(400, {'error': '请求体必须是 JSON 对象'})
            return
        with Handler._lock:
            Handler.received.append(payload)

        # ── 契约校验（缺字段 → 400）──
        missing = [f for f in _REQUIRED_FIELDS if f not in payload]
        if missing:
            self._send(400, {'error': f'缺少必填字段: {",".join(missing)}'})
            return
        if Handler.require_auth and not (self.headers.get('Authorization') or '').startswith('Bearer '):
            self._send(401, {'error': '缺少 Bearer 鉴权头'})
            return

        # ── 故障注入 ──
        if Handler.mode == 'http500':
            self._send(500, {'error': '平台内部错误（注入）'})
            return
        if Handler.mode == 'business_error':
            self._send(200, {'code': 'STOCK_OUT', 'status': 'fail',
                             'error': '花材库存不足（注入）', 'message': None})
            return
        if Handler.mode == 'no_order_id':
            self._send(200, {'status': 'created'})
            return
        if Handler.mode == 'bad_json':
            self._send(200, '<html>not json</html>')
            return

        # ── 幂等：同 request_id 返回既有订单 ──
        request_id = str(payload.get('request_id') or '')
        with Handler._lock:
            if request_id in Handler.orders:
                self._send(200, Handler.orders[request_id])
                return
            Handler._seq += 1
            order_id = f'PMOCK{time.strftime("%Y%m%d%H%M%S")}{Handler._seq:03d}'
        total = payload.get('estimated_total')
        try:
            total = float(total or 0)
        except (TypeError, ValueError):
            total = 0.0
        body = {
            'order_id': order_id,
            'status': 'created',
            'total_price': total,
            'pay': {
                'type': 'miniapp',
                'page_path': '/pages/order/confirm',
                'params': {'order_id': order_id, 'total_price': total,
                           'shop_id': payload.get('shop_id', '')},
            },
        }
        with Handler._lock:
            Handler.orders[request_id] = body
        self._send(200, body)


def main() -> None:
    parser = argparse.ArgumentParser(description='本地 mock 平台下单 API')
    parser.add_argument('--port', type=int, default=4762)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'mock 平台下单 API: http://{args.host}:{args.port}/v1/orders')
    print('agent 侧配置: PLATFORM_ORDER_ENABLED=true '
          f'PLATFORM_ORDER_API_URL=http://{args.host}:{args.port}/v1/orders')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
