# H5 业务后端客户服务契约（v1.4.0 冻结版）

> 状态：**待业务团队评审**。本文档冻结 agent 侧对 H5 业务后端的全部依赖，
> 评审通过后两侧按此实现即可联调；任何字段变更须走版本升级而不是静默修改。
> 日期：2026-10-08。依据：`backend/customer_service.py`、`backend/customer_shops.py`、
> `backend/customer_identity.py`、H5 侧 `server/src/customerService.js`、`customerCredential.js`、
> `customerShops.js` 的现有实现（两侧已按此契约各自实现并单元测试通过）。

## 0. 总原则

- **只读**：客户服务链路只有查询，没有下单、取消、退款提交。智能体不得承诺代办这些操作。
- **凭据隔离**：agent 不接触用户登录 JWT。H5 换发短时「委托票据」（cs1.*），agent 拿票据查询。
- **最小字段**：H5 只返回白名单字段；地址明细、电话、退款审核备注等按接口逐个约定。
- **HTTPS 强制**：票据只发往运维配置的可信 HTTPS 源，不跟随重定向（agent 侧已硬编码）。

## 1. 端点与配置

| 项 | 值 |
|---|---|
| H5 基址 | `CUSTOMER_SERVICE_BASE_URL = https://h5.tiaowulan.com/api/customer-service` |
| 开关 | 两侧同名 `CUSTOMER_SERVICE_ENABLED=true`（默认 false，fail-closed） |
| 共享密钥 | 两侧同名 `CUSTOMER_SERVICE_SECRET`（≥32 字节，独立于登录 JWT 密钥，两侧一致） |

## 2. 委托票据（cs1）

签发方：H5。验证方：H5 自己（agent 只透传，不解密不缓存不落盘）。

格式：`cs1.<base64url(payload)>.<base64url(HMAC-SHA256)>`

payload 字段（H5 `customerCredential.js`）：

| 字段 | 类型 | 约定 |
|---|---|---|
| uid | int>0 | H5 用户主键 |
| aud | string | 固定 `twl-customer-service` |
| scope | string | 固定 `orders:read` |
| iat / exp | int 秒 | **TTL 固定 300 秒**（exp-iat 必须等于 300） |
| jti | string | 随机唯一 ID |

- 签名域独立（不复用登录 JWT 密钥）；客户端不能指定 uid/权限/有效期。
- **票据不能续签自身**：`POST /credential` 只接受真实登录 JWT，cs1 换票请求直接拒绝。
- 使用方式：agent 每轮请求经 `X-Customer-Credential` 头透传给 chat API（见 §6）。

## 3. 接口一：门店公开查询（无需票据）

`GET {base}/shops?city=<城市名>` —— 返回门店候选列表。

H5 返回（`ok/data/trace_id/source/fetched_at` 信封）：

| 字段 | 类型 | 约定 |
|---|---|---|
| items[].id | string | 门店 ID，如 `s007` |
| items[].name | string | 店名 |
| items[].city | string/null | 城市 |
| items[].address | string | 地址 |
| items[].business_hours | string/null | 营业时间原文，如 `08:00-22:00` |
| items[].delivery_policy | string/null | 配送说明（可空，空=未提供） |
| items[].service_phone | string/null | 电话 |
| items[].updated_at | string/null | 数据更新时间 |

`GET {base}/shops/<id>` —— 单店查询，字段同上（单对象非 items 数组）。

agent 侧行为：`query_shop_service` 工具按店铺编号或城市查询；未指定店铺时列候选请用户选择；
**空字段视为「未提供」，不得猜测营业或配送承诺**（已写入 base.md 工具说明）。

## 4. 接口二：本人订单查询（需票据）

`GET {base}/orders?page=<1..10000>`（Bearer cs1 票据）

| 字段 | 类型 | 约定 |
|---|---|---|
| data.items[].id | string | 订单号 |
| data.items[].status | string | 见 §5 状态机 |
| data.items[].shop_name | string | 店名 |
| data.items[].total_price_cents | int | **金额，单位分** |
| data.items[].created_at | string | 下单时间 |
| data.items[].expected_delivery | string/null | 期望送达 |
| data.has_more | bool | 分页标记（每页 10 条，多取 1 条判断） |

`GET {base}/orders/<id>` —— 单笔订单，字段同上。

agent 侧行为：`query_my_orders` 工具；多笔订单先列出让用户选择；
`AUTH_REQUIRED` 时引导登录，**不得说「您没有订单」**（超时/不可用 ≠ 无订单）。

## 5. 接口三：售后进度查询（需票据）

`GET {base}/orders/<id>/after-sales`（Bearer cs1 票据）

订单状态机（H5 `orders.status`，与 agent 展示文案的对应）：

| status | 展示文案 |
|---|---|
| pending | 待支付 |
| paid | 已支付待接单 |
| making | 制作中 |
| delivering | 配送中 |
| completed | 已完成 |
| cancelled | 已取消 |
| refund_applying | 退款审核中 |
| refunding | 退款处理中 |
| refunded | 已退款 |
| refund_failed | 退款失败 |

售后子结构 `data.after_sales`：

| 字段 | 约定 |
|---|---|
| state | `pending_review` / `processing` / `succeeded` / `failed` / `rejected` / `none` / `unknown` |
| merchant_review | `approved` / `rejected` / `unknown` |
| amount_cents | int 分，可空 |
| applied_at / refunded_at | 时间戳，可空 |

agent 侧行为：`query_my_after_sales` 工具；**商家审核通过 ≠ 退款已到账**（已写入工具说明）。

## 6. agent 侧 chat 集成（H5 网关职责）

1. H5 网关以平台身份（X-API-Key）为终端用户换取 agent JWT（`POST /auth/token`）。
2. 用户登录态下，H5 调 `POST /api/customer-service/credential` 用登录 JWT 换 cs1 票据。
3. 调 agent `POST /chat` 时以 `X-Customer-Credential: <cs1 票据>` 头透传票据。
4. agent 校验票据属于当前对话账号（`customer_identity`，不一致 403）后注入工具执行上下文；
   票据**不进入**提示词 / 历史 / 日志 / localStorage。

## 7. 错误码（agent 侧统一口径）

| code | HTTP | 含义 | agent 侧行为 |
|---|---|---|---|
| UNSUPPORTED | 503 | 开关关闭 | 「该功能暂未开放」 |
| AUTH_REQUIRED | 401 | 未登录/票据无效 | 引导登录，不说无订单 |
| FORBIDDEN | 403 | 越权 | 不泄露他人信息 |
| NOT_FOUND | 404 | 订单不存在 | 如实说查不到 |
| RATE_LIMITED | 429 | 限流（可重试） | 请稍后再试 |
| INVALID_ARGUMENT | 400 | 参数错 | 修正参数重试 |
| UPSTREAM_UNAVAILABLE | 5xx | 上游故障（可重试） | 如实说暂时查不到 |
| UPSTREAM_TIMEOUT | - | 超时（可重试） | 同上 |
| INVALID_UPSTREAM_RESPONSE | - | 响应不符合契约 | 同上 |

## 8. 已知差异与待评审点

1. **H5 网关 `AGENT_TARGET` 当前指向 `https://api.tiaowulan.com`（旧部署）**，未指向
   智能体服务器 `49.232.49.176`。联调前需运维在 H5 静态服务 systemd 环境里修正
   （`AGENT_TARGET=https://49.232.49.176`，可选 `AGENT_TLS_SERVERNAME`/`AGENT_HOST_HEADER`
   按证书情况配置）。**此项是 2026-10-08 实测确认的联调阻断点。**
2. 门店接口的 `delivery_policy` 生产数据目前全为 null（12 家店均未配置配送说明）——
   agent 已按「未提供如实转述」处理，业务侧如需补数据请另行排期。
3. cs1 票据 TTL 300 秒：多轮对话中每轮由 H5 重新签发（H5 网关职责，见 §6.2）。
