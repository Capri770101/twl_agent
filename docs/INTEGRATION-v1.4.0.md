# v1.4.x 接入说明（1.4.1 源码候选口径）

> **接口说明，非本版上线证明。** 当前仓库 [`VERSION`](../VERSION) 为 1.4.1；服务器上一轮部署的 1.4.0 不自动获得本文新增的事项查询和提醒管理。本文只根据仓库源码区分“已实现”和“待接入/验证”。H5/商家系统源码不在此包内，不能据 agent 侧适配器宣称其已部署；所有标识和密钥使用占位符。

## 1. 部署、环境与平台数据源

**已实现。** [`main.py`](../main.py) 启动时 `init_db`、检查/恢复生图任务并启动提醒调度，注册 auth、chat、greetings、speech、notifications、metrics 等路由；`GET /health` 只返回 `{"status":"ok"}`。`/generated/*` 从 `DB_PATH` 目录下 `generated/` 提供静态资源；若横向扩容需另行确认共享存储。默认 [`docker-compose.yml`](../docker-compose.yml) 提供 PostgreSQL/agent；nginx/dashboard 属于 `nginx` profile，demo 使用隔离的 demo DB/agent/卷，勿在生产 agent 升级中顺手重建 demo。[`deploy/nginx.conf`](../deploy/nginx.conf) 负责 HTTPS、关闭 SSE 缓冲、反代 API、静态图与 dashboard。

`.env` 不入版本控制：所有非本地环境（包括 staging/pre/uat）配置 PostgreSQL `DATABASE_URL`、随机生成且至少 32 字符的 `JWT_SECRET`（公开占位符会被拒绝）、明确且非空/非 `*` 的 `ALLOWED_ORIGINS`；强制 `AUTH_REQUIRED`。正式客服实例必须显式保持 `ANONYMOUS_LOGIN_ENABLED=false`。如另行部署隔离的匿名体验实例，显式启用匿名时须配置 Redis、正数全局/用户每日 token 预算及正数 `RATE_LIMIT_IP_PER_MINUTE`，缺任一项拒绝启动；匿名预算 Redis 不可用时拒绝模型调用，认证接口按 TCP 对端限流（单进程）。随包 [`deploy/nginx.conf`](../deploy/nginx.conf) 在入口按 `$binary_remote_addr` 对匿名换票 6 次/分钟、体验聊天 30 次/分钟、其他认证 120 次/分钟设限；应用不信用户自填转发头，经过代理时应用看到的是网关地址，`ANONYMOUS_AUTH_RATE_LIMIT_PER_MINUTE`、`AUTH_RATE_LIMIT_PER_MINUTE` 和 `RATE_LIMIT_IP_PER_MINUTE` 都是**代理聚合兜底**，需根据总流量单独设定正数。直接绕开入口的公开部署不受入口逐 IP 限流保护，禁止据此声称体验安全。现有预算是请求前读数、完成后记账的 best-effort 计数：并发请求可短暂超额，**不能保证绝对费用硬上限**。**目前未确定日预算额度，不能仅凭本源码声称匿名公网体验具备硬性费用上限。** `API_DOCS_ENABLED` 默认关闭。模型配置 `LLM_API_KEY/LLM_BASE_URL/LLM_MODEL`；平台后端换票用 `PLATFORM_API_KEYS`（`platform_id=<secret>`，不得下发浏览器）；`PLATFORM_SOURCE_ACCESS` 将已认证平台 ID 映射到被授权数据源，缺失/错误拒绝，单源兼容开关只用于明确的历史单源场景。[`backend/config.py`](../backend/config.py)、[`backend/auth.py`](../backend/auth.py)、[`backend/data_gateway/access.py`](../backend/data_gateway/access.py)

平台商品/门店只读源二选一：`PLATFORM_DB_<SOURCE>_URL` 支持 PostgreSQL/MySQL 只读事务，DB 账号也应只授 SELECT；`PLATFORM_API_<SOURCE>_URL` 与可选 `_TOKEN` 拉公开产品/门店 GET；HTTP 查询将分转元、字段白名单规整、本地按关键词/店铺过滤，默认进程内缓存 60 秒（`PLATFORM_HTTP_CACHE_TTL`），对公开源 `order/user` 不开放。`ENABLE_OPS_TOOLS` 默认关闭，`PLATFORM_ALLOWED_ENTITIES` 可进一步缩小查询实体。[`backend/data_gateway/external.py`](../backend/data_gateway/external.py)、[`backend/data_gateway/http_source.py`](../backend/data_gateway/http_source.py)、[`backend/data_gateway/access.py`](../backend/data_gateway/access.py)、[`backend/config.py`](../backend/config.py)

客服只读适配显式启用 `CUSTOMER_SERVICE_ENABLED`，配置固定可信 `CUSTOMER_SERVICE_BASE_URL` 和独立至少 32 字节 `CUSTOMER_SERVICE_SECRET`；生产强制 HTTPS，不随重定向且不使用环境代理；`CUSTOMER_SERVICE_ALLOW_INSECURE` 只允许本地 dev/test mock，生产即使误配也强制失效。独立 `DASHBOARD_API_KEY` 不配置时 metrics 返回 503。**待验证**：H5 签票服务与只读 GET 真可达、证书/跨域、上游字段和本人授权、生产 DB 建表权限；配置开关不等于联通。[`backend/config.py`](../backend/config.py)、[`backend/customer_service.py`](../backend/customer_service.py)、[`backend/routers/metrics.py`](../backend/routers/metrics.py)

## 2. 普通 JWT 与客服 cs1 的两条身份链

| 用途 | 调用与边界 | 接入方职责 |
|---|---|---|
| 平台普通用户 | 可信平台后端 `POST /auth/token`，请求头 `X-API-Key: <platform-secret>`、JSON `{"external_user_id":"<stable-id>"}`，返回隔离派生 `user_id`、`access_token`、`platform_id`；微信小程序可用 `POST /auth/wx-login` 传 code，`/auth/anonymous` 仅开发开关允许时使用。 | 平台先认证终端用户，服务端保管平台 key；浏览器只使用短/受控 JWT 和返回的 `user_id`。 |
| 本人订单客服 | `POST /auth/customer-token` JSON `{"credential":"<cs1-ticket>"}` 返回 `customer_h5_` 身份 JWT；每次查询本人订单的 chat 再带 `X-Customer-Credential: <same-cs1-ticket>`。 | H5 可信服务端签发 `cs1.<payload>.<HMAC>`：`aud=twl-customer-service`、`scope=orders:read`、有效整数 uid、iat/exp 恰好间隔 300 秒、非空 jti；H5 收到订单 GET 再验票并检查本人归属。 |

JWT HS256 的 `sub` 是 agent `user_id`，`platform` 仅供监控归因；资源请求中 `user_id` 须与 JWT 一致，401 表示未认证/过期、403 表示越权。`customer-token` 只验签票、不自行验证 H5 浏览器登录：签票必须仅由可信 H5 服务端在真实登录后生成。绑定检查防止普通 JWT 加别人的 cs1 冒用；模型不应生成或填写票据。关闭 `AUTH_REQUIRED` 会跳过普通身份比对，绝不用于生产。**敏感事项访问**：通用 agent JWT 当前默认最长 720 小时（见 `JWT_EXPIRE_HOURS`），但本人工单历史 HTTP 与对话查询要求**签发时间不超过 15 分钟**的同人 JWT，超出返回 401 / 工具 `AUTH_REQUIRED`；接入方须在已验证用户登录后重新换发，而不能靠旧 token 继续查询。cs1 的 5 分钟到期及 H5 登出不会自动吊销已经签发且处于这 15 分钟窗口内的 JWT；H5 退出/切换账号必须立即清除 token 与缓存的工单详情、电话和会话数据；若业务要求登出即时失效，还需 H5 与智能体双方实现服务端吊销或按会话绑定。制定投诉及电话的留存/删除政策；不要把客服 JWT 存入不受控终端。[`backend/routers/auth.py`](../backend/routers/auth.py)、[`backend/auth.py`](../backend/auth.py)、[`backend/customer_identity.py`](../backend/customer_identity.py)、[`backend/routers/chat.py`](../backend/routers/chat.py)

## 3. 对话请求、会话和渲染

`POST /chat` 需 Bearer JWT，JSON 最小体 `{"message":"想买生日花束","user_id":"<token-user-id>"}`；可选 `session_id,location,shop_id,entry,product_id,product_title`。商品详情入口传真实商品/所属门店，首页不传虚构门店，`shop_id=default` 被规范为空。已有会话只接受本人，其他/不存在会新建；下一轮传返回 `session_id`。已提供 `POST /conversations`、`GET /conversations?user_id=...`、`GET /conversations/{id}/messages?user_id=...`、`GET /conversations/{id}/plans?user_id=...`、`DELETE /conversations/{id}?user_id=...`、`POST /chat/reset`。[`backend/routers/chat.py`](../backend/routers/chat.py)、[`agent/ports.py`](../agent/ports.py)

非流式成功的结构化 `ChatResponse`：`reply,ui,data,action,tool_calls,session_id,stage,products,speech_text,ai_generated,content_disclosure,user_id`。`products[]` 以 `plan_id,name,price_yuan,image,stock` 渲染；`tool_calls` 仅为工具调用记录，不是付款/退款凭证。界面显著展示 AI 内容标识。`GET /ui-contract` 的基础八组件 `text/dialog_options/plan_card/shop_card/order_card/pay_jump/image_task/greeting_card` 可作渲染核对，但源码 UI 枚举还含 `customer_orders/customer_shops/customer_login`，前端须处理这三卡与未知 UI 的文本降级。`dialog_options.options[].value` 作为下一条消息，DIY `plan_card.plans[].copy_text` 原样复制，参考价非成交价；`action.type/required_capabilities/fallback` 只是平台动作意图，订单与支付的真实结果由宿主核实。[`agent/engine/ui_protocol.py`](../agent/engine/ui_protocol.py)、[`backend/routers/chat.py`](../backend/routers/chat.py)、[`agent/tools_meta.py`](../agent/tools_meta.py)

`POST /chat/stream` 使用同一 JSON 体、Bearer 和（需要本人订单时）cs1 头，响应 `text/event-stream`。事件协议可有 `tool_call`、`text_delta`、`text_rollback`、整段 `text`、`card{ui,data}`、`done{session_id,reply,ui,data,speech_text,ai_generated,content_disclosure}` 或 `error{message}`；**当前主 ReAct 对话为避免泄漏未经证实的承诺，会在服务端流式读取模型，但只在证据校验/清理完成后向客户端发最终整段 `text`**，不得用 `text_delta` 的到达时间作为性能验收标准。少量快速路径/历史协议可能采用不同事件组合；前端始终以 `done.reply` 覆盖之前文字，处理可选的 `text_rollback`。流建立后发生忙碌/失败可返回 `event:error` 而不是非 200 HTTP。浏览器需用可带 POST 自定义头的流式解析，并验证 nginx 不缓冲。[`agent/agent.py`](../agent/agent.py)、[`backend/routers/chat.py`](../backend/routers/chat.py)、[`deploy/nginx.conf`](../deploy/nginx.conf)

## 4. 平台只读、写操作缺口与商家本地联系单

**已实现的只读 adapter**：`query_shop_service` GET 固定可信客服基址 `/shops`、`/shops/{id}`，可按名称本地筛选；不唯一时澄清。`query_my_orders` 对本人 cs1 GET `/orders?page=N` 或 `/orders/{id}`，列表最多 10 笔和 `has_more`；`query_my_after_sales` GET `/orders/{id}/after-sales`。订单字段白名单，错误不能说成“没有订单”。公开平台 product/shop 数据与本人订单授权完全不同。`query_shop_reviews` GET 推导出的 H5 根路径 `/api/shops/{id}/reviews`；即使端点通，也可能返回空数组，应说“暂无评价数据”。H5 实际接口和数据须联调核验。[`agent/customer_tools.py`](../agent/customer_tools.py)、[`backend/customer_service.py`](../backend/customer_service.py)、[`backend/customer_shops.py`](../backend/customer_shops.py)、[`backend/customer_actions.py`](../backend/customer_actions.py)、[`backend/data_gateway/access.py`](../backend/data_gateway/access.py)

**明确未接通**：`cancel_my_order` 和 `apply_refund` 虽为已注册工具，目前直接返回 `CHANNEL_UNAVAILABLE`；H5 用户态取消/退款接口用浏览器 JWT，而 cs1 只有 `orders:read`，不能冒用。`update_order_address` 也直接返回 `CHANNEL_UNAVAILABLE`，目前无可用写端点。先请求用户确认写意图仍不等于执行成功，前端应引导到平台订单页或明确建议联系商家。未来须由 H5 新增可信写代理、单独 write scope、本人校验、幂等/审计并改 adapter；本草案**不声称任何写 URL 已可用**。[`backend/customer_actions.py`](../backend/customer_actions.py)、[`agent/customer_tools.py`](../agent/customer_tools.py)

**本地工单已实现，但不通知商家**：用户明确要求联系店铺时 `contact_merchant` 在 agent 自有 PG 表登记 `merchant_tickets`；`summary` 必填，有 `order_id` 必用本人票据验证；仅唯一命中店铺才给 `service_phone`，否则不编号码。成功返回 `ticket_id,status=open,delivery_scope=local_only,merchant_channel=channel_unavailable,merchant_notified=false,order_ownership_verified`，落库失败不报成功。接入方可用**15 分钟内签发的本人 Bearer JWT** 调 `GET /api/merchant-tickets?limit=20&offset=0` 分页展示摘要、`GET /api/merchant-tickets/{id}` 展示本人详情；用户在对话中追问已登记事项时 `query_my_tickets` 仅在请求入口验证同一新鲜 JWT 后从受信上下文取本人身份，不接受模型填写他人用户 ID。匿名或旧 JWT 返回 401，别人的编号与不存在的编号一律返回 404；列表不返回原始诉求详情和回拨电话。响应始终包含 `delivery_scope=local_only,merchant_delivery_verified=false`，即使数据库里有手工状态也**不是商家签收回执**。商家后台消费、送达、状态推进/回执都属接入方待办，不能把登记说成“商家已收到”。[`backend/merchant_contact.py`](../backend/merchant_contact.py)、[`backend/routers/merchant_tickets.py`](../backend/routers/merchant_tickets.py)、[`backend/storage/db.py`](../backend/storage/db.py)

## 5. 提醒、通知和时区

`set_reminder` 的一次性 `first_run="YYYY-MM-DD HH:MM"` 解释为 **Asia/Shanghai 本地时间**，返回 UTC ISO `next_run`、本地 `next_run_local` 和 `timezone`；周期规则为五字段 cron（分 时 日 月 周），支持数字、`*`、`*/n`、范围、逗号列表，`0=周日`，日和周均受限时按 OR；非法或两年内不触发返回 `INVALID_ARGUMENT`。进程内线程每约 60 秒扫描 due，PG `FOR UPDATE SKIP LOCKED` 事务写 `notifications`，非秒级实时承诺；一次性置 done，周期推进。接入方 JWT 调 `GET /api/notifications?limit=50&offset=0` 获取 `{ok,data:{items,unread}}`、`POST /api/notifications/{id}/read` 标已读；`GET /api/reminders` 查规则及上海时区 `next_run_local`，`POST /api/reminders/{id}/pause|resume` 暂停和恢复，`PATCH /api/reminders/{id}` 传 `{"content":"..."}` 或 `{"cron":"0 9 8 * *"}` 或 `{"first_run":"YYYY-MM-DD HH:MM"}` 修改；`DELETE /api/reminders/{id}` 软删不可恢复。对话中 `manage_my_reminders` 支持 list/pause/resume/update/delete，来自受信上下文的本人身份，操作指定提醒前应先查清编号。恢复周期规则会跳过暂停期间的触发，一次性时间已过必须先改为未来时间；读写按 JWT 用户 WHERE 过滤。**仅站内 inbox**，微信/短信/外部推送待接入方接通；生产必须开启鉴权。[`backend/reminders.py`](../backend/reminders.py)、[`backend/routers/notifications.py`](../backend/routers/notifications.py)、[`main.py`](../main.py)

## 6. 图、语音、贺卡

对话 `image_task` 从 `data.task_id/poll` 带 Bearer 轮询 `GET /tasks/{task_id}`，`processing` 继续、`done` 取 `result_url`、`failed` 展示错误、`not_found` 表示不存在/非本人，**非 `succeeded`**。`IMAGE_PROVIDER` 当前 `mock/qwen/hy` 有实现，其余占位回退 mock；正式验收务必使用真实 provider 验证。`/generated/` 是公开静态资源，不在图里放隐私资料。[`backend/routers/chat.py`](../backend/routers/chat.py)、[`backend/storage/tasks.py`](../backend/storage/tasks.py)、[`backend/config.py`](../backend/config.py)、[`main.py`](../main.py)

`POST /speech/tts` JSON `{user_id,text}` 返回 `text,voice,audio_url,cached,ai_generated`；建议用响应 `speech_text` 作短播报而非念全卡。`POST /speech/transcribe?user_id=...` multipart `file` 返回 `{text,ai_generated}`，前端再把文字送 `/chat`。均验 Bearer 与本人、按用户限流；`SPEECH_ENABLED=false` 返回 503；ASR 限 MIME/字节，上述配置中的 60 秒目前只是前端建议，不是服务端音频时长硬校验；TTS 上游临时签名音频地址下载后本地保存。[`backend/routers/speech.py`](../backend/routers/speech.py)、[`backend/config.py`](../backend/config.py)、[`agent/engine/ui_protocol.py`](../agent/engine/ui_protocol.py)

对话中 `greeting_card` 如返回 `image_url` 可直接展示；**独立** `POST /greetings/draft` 用调用方给的商品摘要/心意生成需人工确认的文案（`context_source=caller_supplied`，不是核实订单事实），`POST /greetings/render` 用确定文案返回 `ui=greeting_card,data.task_id,poll`，须继续按生图任务轮询，不能把该端点误当作同步图片返回。两端点均验 JWT/本人、限流、输入长度；上线核查模板字体及真实图 provider。[`backend/routers/greeting.py`](../backend/routers/greeting.py)、[`agent/engine/ui_protocol.py`](../agent/engine/ui_protocol.py)

## 7. 指标、失败、限流与安全

`GET /api/metrics/summary|calls|platforms|tools|speech|images|customer-service|stream` 汇总调用、错误、平台、工具、语音/图和客服指标；`DASHBOARD_API_KEY` 独立于用户 JWT，支持 `X-Dashboard-Key`、Bearer，SSE EventSource 还可 `?key=`（谨慎处理 URL 日志/浏览历史）；未配 503、错误 401。静态 dashboard 容器不替代 PG 指标与业务健康探针。[`backend/routers/metrics.py`](../backend/routers/metrics.py)、[`backend/observability.py`](../backend/observability.py)、[`docker-compose.yml`](../docker-compose.yml)

- `/chat` 用户默认每分钟 30 次进程内固定窗口，按需加 `RATE_LIMIT_IP_PER_MINUTE`；429 带 `Retry-After`。并发槽满 503，执行超时 504；流式已经建立后错误是 `event:error`。多实例计数不共享，重启清零；前端按提示退避并避免无脑重试。token 日预算在普通认证用户链路中仍需 `LLM_COST_ENABLED`、Redis URL 和正预算同时具备，否则静默放行，不能当作已有硬性全局成本上限；**外网匿名体验例外**：启动配置强制上述条件，Redis 读预算故障时拒绝匿名请求；计量仍属调用后累计，不能保证精确阻止单个并发请求越过余额。[`backend/rate_limit.py`](../backend/rate_limit.py)、[`backend/routers/chat.py`](../backend/routers/chat.py)、[`backend/config.py`](../backend/config.py)、[`agent/engine/budget.py`](../agent/engine/budget.py)
- 客服 adapter 返回结构化 `AUTH_REQUIRED/FORBIDDEN/NOT_FOUND/RATE_LIMITED/UPSTREAM_TIMEOUT/UPSTREAM_UNAVAILABLE/INVALID_UPSTREAM_RESPONSE/UNSUPPORTED` 与适用的 `retryable`；不得把失败说成无订单、已退款。客服源固定且 HTTPS、不随跳转、不用环境代理、白名单保留字段；生产密钥不得进浏览器/文档/日志，CORS 不通配，公开 API docs 默认关，平台源须明确授权，外部 DB 账号只读。[`backend/customer_service.py`](../backend/customer_service.py)、[`backend/customer_shops.py`](../backend/customer_shops.py)、[`backend/config.py`](../backend/config.py)、[`main.py`](../main.py)

## 8. 上线、回滚、测试交接

1. **先核基线并备份**：仓库版本不证明现网版本；记录目标环境运行版本、镜像摘要、配置键名（不记录值），备份生产 PG、图片持久数据、现用镜像/前端静态产物和安全保存的环境配置。`init_db` 启动会运行 [`db.py`](../backend/storage/db.py) 的**完整 schema DDL**（包括工单和提醒表及索引）；[`001_image_tasks.sql`](../migrations/001_image_tasks.sql) 只处理图片任务，**不能代替完整初始化，也不意味着仅有 DML 权限的账号就能启动**。DBA 限权环境须先评审完整 schema 和启动 DDL 策略，预演权限，再发布；不要直接在生产“试一下”。[`main.py`](../main.py)
2. **依赖先行，按组件灰度**：H5 可信签票和只读接口、本人生效与越权都验证后再启用 `CUSTOMER_SERVICE_ENABLED`；按当前 Compose 拓扑**只重建生产 agent**，未经变更授权不触动隔离 demo；仅当 nginx/dashboard 自身变更才按变更单更新。启动后验证 HTTPS `/health`、SSE、数据库与上游，不用另一套临时服务冒充现有入口。[`docker-compose.yml`](../docker-compose.yml)、[`deploy/nginx.conf`](../deploy/nginx.conf)、[`releases/v1.4.0/UPGRADE.md`](../releases/v1.4.0/UPGRADE.md)
3. **自动与人工验收**：测试依赖就绪后执行 `python -m pytest -q` 并记录环境/退出码；联测普通与 cs1 本人/跨人/篡改/过期、商品价单位与源授权、订单/售后读取和失败码、无/多门店工单、不发送商家通知、写通道 `CHANNEL_UNAVAILABLE`、SSE 最终 `done.reply`、图像状态/属主、语音/贺卡、提醒 UTC/上海时间和通知已读、429/503/504、指标密钥。H5 真实接口与商家联动由各接入方签收，模拟数据不能冒充正式验收。[`tests/`](../tests/)、[`backend/routers/chat.py`](../backend/routers/chat.py)、[`backend/customer_actions.py`](../backend/customer_actions.py)
4. **回滚**：先关客服新功能/灰度入口，再回退生产 agent 镜像及实际变更的静态资源，验证旧路由、会话与 SSE；保留新增表与工单/提醒数据供追踪，绝不未经兼容性评估自动删表或整库恢复覆盖上线后业务数据。交接清单注明构建与配置版本（不含密钥）、用例证据、未接写通道和外部通知、责任人及回滚触发条件。[`releases/v1.4.0/UPGRADE.md`](../releases/v1.4.0/UPGRADE.md)、[`releases/v1.4.0/HANDOFF-CHECKLIST.md`](../releases/v1.4.0/HANDOFF-CHECKLIST.md)

## 9. 对接样例与分工（占位符，勿粘贴真实密钥）

下面 `BASE` 是接入方提供的 HTTPS 智能体入口；`<platform-key>` 只允许保存在宿主服务端，`<jwt>`、`<cs1>` 不得写入前端日志、URL 或源码：

```http
POST ${BASE}/auth/token
X-API-Key: <platform-key>
Content-Type: application/json

{"external_user_id":"<已登录用户的稳定ID>"}
```

平台后端将返回的 `user_id` 和 `access_token` 与**本平台用户**绑定；之后每轮对话带同一 Bearer。H5 本人订单场景改为由已登录 H5 服务端签发只读 cs1，先请求 `POST /auth/customer-token`（请求体 `{"credential":"<cs1>"}`）得到 `customer_h5_` 身份，再在对话请求中同时携带该身份 JWT 和相同 cs1，不要混用普通平台 JWT：

```http
POST ${BASE}/chat
Authorization: Bearer <jwt>
X-Customer-Credential: <cs1，仅本人订单场景>
Content-Type: application/json

{"user_id":"<jwt 中的 sub>","message":"我的订单到哪了？","session_id":"<上一轮返回值，可省略>"}
```

前端呈现 `reply` 与 `ui/data`；下轮沿用 `session_id`。要流式，把路径换为 `/chat/stream`，按 SSE 事件逐行解析，并以最终 `done.reply`、`done.ui`、`done.data` 覆盖中间 UI；不得将 `tool_call` 或 `action` 当成交易成功通知。通知读取示例（**由宿主客户端轮询，不等于系统已主动推送**）：

```http
GET ${BASE}/api/notifications?limit=50&offset=0
Authorization: Bearer <jwt>

# 响应结构：{"ok":true,"data":{"items":[{"id":"...","type":"reminder","title":"...","body":"...","is_read":false,"created_at":"..."}],"unread":1}}
```

| 交付角色 | 必须完成的对接与签收 | 当前不能据此宣称 |
|---|---|---|
| 智能体服务 | 在目标提交构建生产 agent、应用内部表、保持 JWT/数据源权限与日志脱敏、发出结构化卡片和通知收件箱 | 不代 H5 办理真实退款、订单修改或代商家接单 |
| H5/平台后端 | 服务端保存平台 key、验证终端登录后换 token；本人订单需签/验只读 cs1；按订单属主返回只读状态，并核对价格单位/状态码 | 不能把浏览器 JWT 暴露给智能体后端作为未授权写接口替代 |
| H5/小程序前端 | 渲染全部 UI 类型及未知类型文本降级，显示 AI 标识；处理 SSE 最终覆盖、认证过期、图任务轮询、站内通知列表/已读 | 工单本地留痕≠商家已收到；站内 inbox≠推送已送达 |
| 商家系统/运营 | 若确有转人工需求，设计工单 API 或安全消费 `merchant_tickets`，投递后回写回执、重试和状态；未接入前提供真实门店联系渠道 | 不允许仅凭本地 `ticket_id` 宣称已联系商家 |
| 未来 H5 写接口 | 独立写 scope、订单属主和状态校验、动作及参数显式确认、幂等/审计、故障恢复、线上权限测试 | 目前取消/退款/改址工具均为 `CHANNEL_UNAVAILABLE` |

**上线交接证据**：保留目标提交及归档 SHA-256、旧镜像和 PG 备份位置（只记录路径，不复制密钥）、变更前后生产 `GET /health` 与容器状态、受控普通/客服身份与越权用例结果、H5 和商家对各自通道的签收或未接通声明。没有对应证据的部分在发布单中明确标为“未验收”，不可通过文档样例自行视为已联通。
