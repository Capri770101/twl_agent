#!/usr/bin/env python3
"""本地 mock 平台只读数据源 —— 离线跑 buying/shop 评测场景用。

为什么需要它
------------
`platform_db_query_entity` 走 `PLATFORM_API_<SOURCE_ID>_URL` 的只读 REST 源
（见 backend/data_gateway/http_source.py）。真实平台源（aistore）是外部依赖，
联调/评测环境未必可达。本脚本按 http_source 的契约实现两个只读列表端点，供
`scripts/run_eval.py` 的 buying 场景离线跑出真实商品卡：

    GET {base}/v1/merchant/products → {"code":0,"data":{"list":[...]}}
    GET {base}/v1/merchant/shops    → {"code":0,"data":{"list":[...]}}

字段用平台 camelCase、金额单位为「分」（http_source 会 ÷100 转元并转 snake_case）。
商品覆盖常见送花场景（生日/表白/母亲节/毕业/开业）与色系（白绿/香槟/粉/红），
保证评测关键词能命中。数据为**演示虚构**，非真实在售商品。

用法::

    python scripts/mock_platform_source.py --port 4763
    # agent 侧：PLATFORM_API_AISTORE_URL=http://127.0.0.1:4763
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 金额单位：分（http_source 的 _cents_to_yuan 会 ÷100）。
PRODUCTS = [
    {'id': 'P001', 'name': '白绿小夜曲·生日花束', 'price': 15800, 'originalPrice': 19800,
     'subtitle': '白绿色系清新雅致', 'description': '白玫瑰配绿色尤加利，适合送对象生日',
     'flowerMeaning': '纯洁的爱', 'flowers': ['白玫瑰', '尤加利叶'], 'image': '/img/p001.jpg',
     'tags': '生日,白绿,清新,对象', 'rating': 4.9, 'sales': 320, 'stock': 50,
     'season': '四季', 'ownerShopId': 's001', 'status': 1},
    {'id': 'P002', 'name': '香槟暖意·告白花束', 'price': 19900, 'originalPrice': 25900,
     'subtitle': '香槟玫瑰温柔高级', 'description': '香槟玫瑰配满天星，表白生日两相宜',
     'flowerMeaning': '我只钟情于你', 'flowers': ['香槟玫瑰', '满天星'], 'image': '/img/p002.jpg',
     'tags': '生日,香槟,告白,对象', 'rating': 4.8, 'sales': 540, 'stock': 30,
     'season': '四季', 'ownerShopId': 's001', 'status': 1},
    {'id': 'P003', 'name': '粉漾梦境·闺蜜生日', 'price': 12800, 'originalPrice': 15800,
     'subtitle': '粉色系甜美', 'description': '粉玫瑰配洋桔梗，适合送闺蜜',
     'flowerMeaning': '甜蜜', 'flowers': ['粉玫瑰', '洋桔梗'], 'image': '/img/p003.jpg',
     'tags': '生日,粉色,闺蜜', 'rating': 4.7, 'sales': 210, 'stock': 40,
     'season': '四季', 'ownerShopId': 's002', 'status': 1},
    {'id': 'P004', 'name': '热恋红玫·表白99枝', 'price': 29900, 'originalPrice': 39900,
     'subtitle': '红色系热烈', 'description': '99枝红玫瑰，热烈告白',
     'flowerMeaning': '热恋', 'flowers': ['红玫瑰'], 'image': '/img/p004.jpg',
     'tags': '表白,红色,对象,纪念', 'rating': 4.9, 'sales': 180, 'stock': 20,
     'season': '四季', 'ownerShopId': 's002', 'status': 1},
    {'id': 'P005', 'name': '康乃馨暖爱·母亲节', 'price': 16800, 'originalPrice': 20800,
     'subtitle': '感恩母爱', 'description': '粉康乃馨配绿叶，送妈妈长辈',
     'flowerMeaning': '伟大的母爱', 'flowers': ['康乃馨'], 'image': '/img/p005.jpg',
     'tags': '母亲节,康乃馨,妈妈,长辈', 'rating': 4.9, 'sales': 620, 'stock': 60,
     'season': '四季', 'ownerShopId': 's003', 'status': 1},
    {'id': 'P006', 'name': '向日葵阳光·毕业季', 'price': 13900, 'originalPrice': 16900,
     'subtitle': '明亮祝福', 'description': '向日葵配尤加利，毕业开业皆宜',
     'flowerMeaning': '沉默的爱与阳光', 'flowers': ['向日葵'], 'image': '/img/p006.jpg',
     'tags': '毕业,向日葵,开业,祝福', 'rating': 4.8, 'sales': 300, 'stock': 35,
     'season': '夏', 'ownerShopId': 's003', 'status': 1},
    {'id': 'P007', 'name': '百合清雅·开业祝贺', 'price': 18800, 'originalPrice': 23800,
     'subtitle': '白色纯净', 'description': '白百合配绿植，祝贺开业乔迁',
     'flowerMeaning': '百年好合', 'flowers': ['白百合'], 'image': '/img/p007.jpg',
     'tags': '开业,祝贺,百合,白', 'rating': 4.7, 'sales': 150, 'stock': 25,
     'season': '四季', 'ownerShopId': 's001', 'status': 1},
    {'id': 'P008', 'name': '郁金香缤纷·纪念日', 'price': 25800, 'originalPrice': 32800,
     'subtitle': '混色高级', 'description': '混色郁金香，纪念日惊喜',
     'flowerMeaning': '爱的告白', 'flowers': ['郁金香'], 'image': '/img/p008.jpg',
     'tags': '纪念日,郁金香,对象', 'rating': 4.8, 'sales': 260, 'stock': 18,
     'season': '春', 'ownerShopId': 's002', 'status': 1},
]

SHOPS = [
    {'id': 's001', 'name': '千百度花坊', 'city': '舟山', 'address': '浙江省舟山市定海区东海东路122号',
     'phone': '13567675068', 'rating': 4.9, 'ratingCount': 1280, 'status': 1,
     'businessHours': '07:00-22:00', 'deliveryFee': 0, 'minOrderPrice': 9900,
     'deliveryTime': '同城2小时达', 'monthSales': 860, 'description': '主打清新雅致花束'},
    {'id': 's002', 'name': '花诺花艺', 'city': '福州', 'address': '福建省福州市台江区上海街道西环中路674-6号',
     'phone': '18650717901', 'rating': 4.8, 'ratingCount': 960, 'status': 1,
     'businessHours': '08:00-22:00', 'deliveryFee': 500, 'minOrderPrice': 12800,
     'deliveryTime': '同城3小时达', 'monthSales': 640, 'description': '表白纪念日主题'},
    {'id': 's003', 'name': '花美家AI花店', 'city': '广州', 'address': '广州市天河区五山街道大丰路自编18号B栋',
     'phone': '13923439966', 'rating': 4.9, 'ratingCount': 2100, 'status': 1,
     'businessHours': '08:00-22:00', 'deliveryFee': 0, 'minOrderPrice': 9900,
     'deliveryTime': '同城2小时达，满99免运费', 'monthSales': 1320, 'description': '母亲节毕业季热销'},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send(self, rows):
        body = json.dumps({'code': 0, 'message': 'ok', 'data': {'list': rows}},
                          ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split('?', 1)[0].rstrip('/')
        if path.endswith('/v1/merchant/products'):
            self._send(PRODUCTS)
        elif path.endswith('/v1/merchant/shops'):
            self._send(SHOPS)
        elif path in ('', '/health'):
            self._send([])
        else:
            self.send_response(404)
            self.end_headers()


def main() -> None:
    ap = argparse.ArgumentParser(description='本地 mock 平台只读数据源')
    ap.add_argument('--port', type=int, default=4763)
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'mock 平台数据源: http://{args.host}:{args.port}/v1/merchant/products')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
