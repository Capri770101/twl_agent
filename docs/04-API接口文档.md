# 04 · API 接口文档

## 文档信息

| 项 | 值 |
|---|---|
| 文档版本 | v1.0 |
| 编写日期 | 2026-09-07 |
| 适用代码版本 | git HEAD `1a72ab6` |
| 维护人 | 平台后端组 |
| 服务地址 | `https://api.tiaowulan.com`；容器 agent 监听 8000，**公网已收口，仅 `127.0.0.1:8000` 可达**，由 Nginx 反代 443 |
| 校验方式 | 字段逐条取自 `backend/routers/chat.py`、`auth.py`、`metrics.py`、`main.py`、`backend/auth.py`、`agent/engine/ui_protocol.py`；已用线上 `GET /ui-contract`、`GET /health` 实测复核 |

## 0. 通用约定

| 项 | 值 |
|---|---|
| Base URL | `https://api.tiaowulan.com`（路径不带 `/api` 前缀，`/api/metrics/*` 除外） |
| 交互文档 | `https://api.tiaowulan.com/docs`（FastAPI Swagger，可自测） |
| 鉴权头 | `Authorization: Bearer <access_token>` |
| 平台密钥头 | `X-API-Key: <平台密钥>`（**仅 `/auth/token`，只能由宿主后端发起**） |
| 静态资源 | `/generated/{task_id}.png` —— 生图与贺卡结果 |
| 错误结构 | `{"detail": "..."}`；422 为 `{"detail":[{"loc":[...],"msg":"...","type":"..."}]}` |

### 0.1 耗时预期（定超时前先看）

| 链路 | 实测 | 说明 |
|---|---|---|
| `POST /chat` 简单问答 | 3~8 s | 不触发工具或只触发 1 次检索 |
| `POST /chat` 典型 | **约 15 s** | LLM 为阿里云百炼 `qwen3.8-flash`（**带推理**），reasoning token 先行消耗 |
| 生图任务 | **约 54 s** | `qwen-image-3.0`，走百炼原生 `multimodal-generation` 异步任务 |
| 服务端硬超时 | 180 s | `REQUEST_TIMEOUT`（`backend/config.py:127`），超时返回 504 |

> ⚠️ 小程序 `wx.request` **默认超时 60 s**，典型 15 s 虽安全，工具链长时会逼近上限，**建议显式 `timeout: 120000`**。

## 1. 鉴权链路

### 1.1 三种接入方式

| 方式 | 端点 | 生产可用性 |
|---|---|---|
| 平台换票 | `POST /auth/token` + `X-API-Key` | ✅ **主用**（小程序/H5/App） |
| 微信登录 | `POST /auth/wx-login`（code 换票） | ⚠️ 当前未配 `WECHAT_APPID` → 503 |
| 匿名 | `POST /auth/anonymous` | ❌ 生产强制关闭（`config.py:47-48`）→ 403 |

### 1.2 标准链路（小程序必走）

```
小程序(微信登录态) → 宿主后端(验证自家登录态) → POST /auth/token [X-API-Key]
   ← access_token(JWT) + user_id(派生值) → JWT 下发小程序 → 后续请求带 Bearer
```

> **密钥边界**：`PLATFORM_API_KEYS` 是服务端密钥，放进小程序等于把整个智能体后门公开。`X-API-Key` 只能出现在宿主后端的出网请求里。

### 1.3 派生 user_id 与 403（最高频事故点）

`derive_platform_user_id()`（`backend/auth.py:136-143`）：`user_id = f"{platform_id}_{sha256(platform_id:external_user_id)[:24]}"`

- 示例：`wxmini` + `oABC123...` → `wxmini_be1ed470939b8215110d849c`
- 不落盘原始 openid/手机号；轮换 API Key 不影响 `user_id` 稳定性

`require_user()`（`backend/auth.py:87-89`）比较**请求体 `user_id`** 与 **JWT `sub`**，不等即 403。

> ⚠️ **必须原样回传 `/auth/token` 返回的 `user_id`**。自造、用 openid 原文、用自家主键，全部返回 **403**（不是 401——凭证有效，只是人不匹配）。
> ⚠️ JWT 有效期 `JWT_EXPIRE_HOURS=720`（30 天），过期为 **401** `登录凭证无效或已过期`，需重新换票。

## 2. 端点详解

### 2.1 `POST /auth/token` — 平台用户换票

- **鉴权**：`X-API-Key` 头（平台级）
- **请求体**：`external_user_id`（string，必填，1~128 字符，宿主体系内该用户唯一标识）

```http
POST /auth/token
X-API-Key: <平台密钥，仅宿主服务端持有>
{"external_user_id": "oXyz1234567890abcdef"}
```

```json
{"access_token": "eyJhbGciOiJIUzI1NiIs...", "token_type": "bearer",
 "user_id": "wxmini_be1ed470939b8215110d849c", "platform_id": "wxmini"}
```

| 码 | detail | 条件 |
|---|---|---|
| 401 | `平台凭证无效，请检查 X-API-Key` | Key 缺失/不匹配（`auth.py:131`） |
| 503 | `服务未配置 PLATFORM_API_KEYS…` | 服务端未配（`auth.py:126`） |
| 422 | 校验详情 | 字段缺失或超长 |

### 2.2 `POST /auth/wx-login` / `GET /auth/me` — 其余鉴权端点

| 端点 | 鉴权 | 请求 | 200 响应 |
|---|---|---|---|
| `POST /auth/wx-login` | 无 | `{"code": "wx.login() 返回的 code"}`（1~512 字符） | `{"access_token": "…", "token_type": "bearer", "user_id": "wx_9f2c1b7d4e8a3c5f6b0d1e2a"}` |
| `GET /auth/me` | Bearer（可选） | — | `{"authenticated": true, "user_id": "wxmini_be1ed…"}`；未登录 `{"authenticated": false}` |

- `user_id = 'wx_' + sha256(openid)[:24]`（`auth.py:166`），**openid 原文不外泄**
- 当前生产未配 appid → **503**；微信侧 errcode → **401** `微信登录失败，请重试`（原始 errmsg 不透传）
- `/auth/me` 用途：小程序启动判断本地 JWT 是否有效，失效则回宿主后端重新换票

### 2.4 `POST /chat` — 对话主接口（非流式）

- **鉴权**：Bearer + 请求体 `user_id` 必须与 JWT `sub` 一致

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `message` | string | ✅ | 本轮消息；卡片按钮交互也以「用户消息」回传（如「换一款」） |
| `user_id` | string | ✅ | `/auth/token` 返回的派生值 |
| `session_id` | string | ❌ | **多轮上下文全靠它**；不传 = 新会话 |
| `location` | object | ❌ | `{"lat":22.55,"lng":114.06}`；传了才能按距离推荐店铺 |
| `shop_id` | string | ❌ | 从指定店铺页进入时传，会话内锁定不变 |

```json
{"user_id": "wxmini_be1ed470939b8215110d849c", "message": "我想送花给朋友，预算200左右",
 "session_id": "fee6e395a3db41a0acaec1228bcb95e1", "location": {"lat": 22.55, "lng": 114.06}}
```

**200 响应**（`ChatResponse`，`agent/engine/ui_protocol.py:58-69`）：

```json
{
  "user_id": "wxmini_be1ed470939b8215110d849c", "reply": "为你推荐 3 个适合送朋友的方案：", "ui": "plan_card",
  "data": {"plans": [{"plan_id": "P001", "name": "生日玫瑰花束", "price": 199.0, "desc": "红玫瑰 11 支 + 满天星", "effect_image_url": "", "merchant_name": "向阳花艺"}]},
  "action": {"type": "show_plan", "payload": {"ui": "plan_card", "stage": "plan_confirm"},
    "required_capabilities": ["show_plan_page"], "fallback": "当前平台暂未实现对应能力，请使用文本方式继续引导。"},
  "tool_calls": [{"name": "platform_db_query_entity", "arguments": {"entity": "plan"}, "result": "…", "status": "ok"}],
  "session_id": "fee6e395a3db41a0acaec1228bcb95e1", "stage": "plan_confirm",
  "products": [{"plan_id": "P001", "name": "生日玫瑰花束", "price_yuan": 199.0, "image": "https://cdn.xxx/rose.png", "stock": 12}]
}
```

| 字段 | 说明 |
|---|---|
| `reply` | 必渲染的中文文本；**组件缺失时也要展示它** |
| `ui` | UI 类型（8 种之一），决定如何渲染 `data` |
| `data` | 与 `ui` 一一对应的结构化数据 |
| `action.type` / `required_capabilities` / `fallback` | 平台中立动作 / 所需能力 / 能力缺失降级文案 |
| `tool_calls` | 本轮实际调用的工具（调式用） |
| `session_id` | **必须保存并回传**，否则下一轮是新会话 |
| `stage` | 会话进度（`analyze`/`plan_confirm`/`shop_recommend`/`image_gen`/`done`…） |
| `products` | 顶层商品卡数组（`plan_id`/`name`/`price_yuan`/`image`/`stock`） |

- **会话行为**（`chat.py:69-75`）：传入的 `session_id` **不存在或不属于该 user** 时**不报错**，静默新建会话并返回新 `session_id`。前端应始终用响应里的值覆盖本地。
- **错误码**：401、403、**504** `处理超时，请简化问题后重试`（>180s）、**500** `智能体执行失败，请稍后重试`
- ⚠️ LLM 自身报错（额度/限流）**不会变成 5xx**：`agent.run()` 捕获后写进 `reply`，HTTP 仍 200

### 2.5 `POST /chat/stream` — SSE 流式对话

- **鉴权/请求体**：与 `/chat` **完全一致**，只改路径
- **响应**：`text/event-stream`，每条 `event: <类型>\ndata: <JSON>\n\n`
- ⚠️ **线上现状：`/chat/stream` 零调用**，宿主一直在用非流式 `/chat`；改造是待办，端点已可用

| 事件 | 载荷 | 前端动作 |
|---|---|---|
| `tool_call` | `name`, `status` | 显示「正在查询…」（体感改善最大，强烈建议） |
| `text` | `content` | **追加**到当前气泡（不是替换） |
| `card` | `ui`, `data` | 按 `ui` 渲染；**收到 card 后不要再解析 text** |
| `done` | `session_id` | 本轮结束 → **保存 `session_id`** |
| `error` | `message` | 展示错误 + 保留重试入口 |

```
event: tool_call
data: {"name":"platform_db_query_entity","status":"ok"}

event: text
data: {"content":"这几束都很适合送朋友，"}

event: card
data: {"ui":"plan_card","data":{"plans":[{"plan_id":"P001","name":"生日玫瑰花束","price":199.0}]}}

event: done
data: {"session_id":"fee6e395a3db41a0acaec1228bcb95e1"}
```

**实现约束**：`text` 按句号/感叹号/问号/换行或 20 字切句（`agent.py:232-241`），**不保证语义完整**；`card` **仅在 `ui != 'text'` 时推送**（`agent.py:243`），纯文本答复只有 `text` + `done`。流内异常只以 `event: error` 结尾（HTTP 头已发出，状态码恒 200）。小程序无 `EventSource`，实现要点与 4 个坑见 [05-前端对接契约 §3](./05-前端对接契约.md)。

### 2.6 `GET /conversations` — 会话列表

- **鉴权**：Bearer + query `user_id`（必须与 JWT `sub` 一致，否则 403）
- **200**：`[{"id": "fee6e395a3db41a0acaec1228bcb95e1", "title": "我想送花给朋友", "preview": "我想送花给朋友", "shop_id": "", "created_at": "2026-09-07T01:50:21+00:00", "updated_at": "2026-09-07T01:52:03+00:00"}]`
- 字段来自 `memory.list_conversations()`（`memory.py:202-205`），**主键字段名为 `id`**（其值即 `session_id`）。线上历史样本中亦见 `session_id`/`user_id`/`stage`，前端取值建议 `item.session_id || item.id` 兼容。按 `updated_at DESC` 排序。

### 2.7 `GET /conversations/{conversation_id}/messages` — 历史消息

- **鉴权**：Bearer + query `user_id`；`limit` 默认 50，夹取到 `1~200`（`chat.py:170`）

```json
[
  {"role": "user", "content": "我想送花给朋友"},
  {"role": "assistant", "content": "为你推荐 3 个方案：", "ui": "plan_card",
   "data": {"plans": [{"plan_id": "P001", "name": "生日玫瑰花束", "price": 199.0}]}}
]
```

- `role`：`user` / `assistant`；**`tool` 角色已过滤**（`memory.py:168`）
- ⚠️ `ui` 经 `json.loads` 后返回**字符串**，但 DB 原始列是**双层编码**的 JSON（形如 `"\"text\""`）。前端需兼容解析，见 [05 §4.2](./05-前端对接契约.md)
- 会话不存在或不属于该 user → **403** `无权访问该会话`

### 2.8 `POST /conversations` — 新建会话

- **请求体**：`{"user_id": "wxmini_xxx", "title": "新对话", "shop_id": null}`（仅 `user_id` 必填）
- **200**：`{"conversation_id": "a1b2c3…", "id": "a1b2c3…"}`（两字段同值）；错误码 401/403/422

### 2.9 `POST /chat/reset` — 清空会话

- **请求体**：`{"user_id": "wxmini_xxx", "session_id": "<可选>"}`
- **行为**：传 `session_id` 删指定会话；不传则删该用户**最近一条**（`chat.py:151-154`）。级联清理 `messages`
- **200**：`{"ok": true, "session_id": "fee6e395…"}`；无可删会话时 `{"ok": true}`；错误码 401/403

### 2.10 `GET /tasks/{task_id}` — 生图任务轮询

- **鉴权**：`AUTH_REQUIRED=true` 时必须带 Bearer，否则 401（`chat.py:188`）
- **归属**：按 `task_id + user_id` 精确匹配（`tasks.py:216-219`），查不到返回 `status: "not_found"`

```json
{"task_id": "55f7e131465f4563", "user_id": "wxmini_be1ed470939b8215110d849c", "status": "done",
 "prompt": "红玫瑰 11 支 + 满天星…", "result_url": "/generated/55f7e131465f4563.png", "error": null,
 "created_at": "2026-09-07T02:10:00+00:00", "updated_at": "2026-09-07T02:10:54+00:00"}
```

| `status` | 含义 | 前端动作 |
|---|---|---|
| `processing` | 生成中（实测约 54 s） | 继续轮询，建议 2 s |
| `done` | 成功，带 `result_url` | 停止轮询，展示图片 |
| `failed` | 失败，带 `error` | 停止轮询，提示重试 |
| `not_found` | 任务不存在或不属于当前用户 | **立即停止**，不要无限轮询 |

> ⚠️ 成功值是 **`done`**，不是 `completed`。
> ⚠️ 服务重启时遗留 `processing` 会被批量置 `failed`（`tasks.py:56-62`）。
> ⚠️ `result_url` 为**相对路径**（`IMAGE_PUBLIC_BASE_URL` 当前为空），前端必须拼 `https://api.tiaowulan.com`（已实测 200）。

### 2.11 免鉴权端点：`GET /ui-contract` / `GET /health`

`/ui-contract` 返回 8 种 UI 类型的 `ui` / `action_type` / `required_capabilities` / `render` / `example`：

```bash
curl -s https://api.tiaowulan.com/ui-contract | jq '.required_components'
```

```json
{"ui_types": [
 {"ui": "text", "action_type": "show_text", "required_capabilities": [], "render": "文本气泡，渲染 reply 即可", "example": {}},
 {"ui": "dialog_options", "action_type": "show_options", "required_capabilities": ["show_options"],
  "render": "选项按钮；点选后把 value 作为下一条消息回传 /chat",
  "example": {"options": [{"label": "现货花束（约200元）", "value": "existing"}, {"label": "DIY 定制", "value": "diy"}]}}],
 "required_components": ["text","dialog_options","plan_card","shop_card","order_card","pay_jump","image_task","greeting_card"],
 "contract_doc": "FRONTEND_CONTRACT.md", "schema_source": "agent/engine/ui_protocol.py",
 "note": "本后端只产出结构化 ui/data/action，前端渲染由宿主平台负责。"}
```

`/health`：`{"status": "ok", "service": "flora-agent", "version": "1.0.0", "env": "prod"}`

### 2.13 `GET /api/metrics/*` — 调用监控（受 `DASHBOARD_API_KEY` 保护）

- **鉴权**：`X-Dashboard-Key: <key>` 或 `Authorization: Bearer <key>`
- **未配置 `DASHBOARD_API_KEY` → 一律 503**（`metrics.py:38-39`）；**错误 Key → 401**

| 端点 | Query | 返回 |
|---|---|---|
| `/api/metrics/summary` | — | 总调用量、近 24h 调用/错误、活跃平台/用户、平均延迟 |
| `/api/metrics/calls` | `hours`(1~720, 默认 24)、`bucket`(`minute`/`hour`/`day`) | `[{bucket,total,errors}]` |
| `/api/metrics/platforms` | — | 各平台累计/近 24h 调用、独立用户、最近调用、错误数 |
| `/api/metrics/tools` | — | 各工具 total/success/errors/avg_latency_ms |
| `/api/metrics/stream` | — | SSE，每 2 s 推送 `call_logs` 新增行 |

```json
{"total_calls": 1284, "calls_24h": 96, "errors_24h": 3,
 "active_platforms_24h": 2, "active_users_24h": 17, "avg_latency_24h": 15230}
```

> ⚠️ **已知缺陷**：`_verify()`（`metrics.py:49-58`）**只读 Header，不读 query `?key=`**，而 `dashboard/app.js:266` 建 SSE 用的正是 `?key=`，浏览器 `EventSource` 又无法自定义 Header → **面板实时流当前必然 401**。支持 query 的 `require_dashboard()` 目前无任何端点引用。修复前请用带 Header 的客户端验证（见 [09 §7](./09-测试与验收.md)）。

## 3. 错误码对照表

| 码 | detail | 出处 | 处置 |
|---|---|---|---|
| 401 | `需要 Bearer 登录凭证` | `auth.py:74` | 请求头缺失或前缀非 `Bearer ` |
| 401 | `登录凭证无效或已过期` | `auth.py:47` | JWT 过期（30 天）→ 重新换票 |
| 401 | `平台凭证无效，请检查 X-API-Key` | `auth.py:132` | 平台密钥错/放错环境 |
| 401 | `微信登录失败，请重试` | `auth.py:164` | code 失效；errcode 不透传 |
| 401 | `监控密钥无效` | `metrics.py:46,58` | `DASHBOARD_API_KEY` 不对 |
| 403 | `无权访问其他用户的数据` | `auth.py:89` | **请求体 `user_id` ≠ JWT `sub`** → 用派生值 |
| 403 | `无权访问该会话` | `chat.py:169` | 会话不属于该用户 |
| 403 | `匿名登录已禁用…` | `auth.py:57` | 生产禁匿名，走平台换票 |
| 422 | pydantic 校验详情 | FastAPI | 看 `detail[].loc` |
| 500 | `智能体执行失败，请稍后重试` | `chat.py:88` | 查容器日志 traceback |
| 503 | `服务未配置 PLATFORM_API_KEYS…` | `auth.py:127` | 服务端配置缺失 |
| 503 | `未配置 WECHAT_APPID/WECHAT_SECRET` | `auth.py:148` | 未启用微信登录（线上当前态） |
| 503 | `监控面板未启用：请在 .env 配置 DASHBOARD_API_KEY` | `metrics.py:39` | 监控未启用 |
| 504 | `处理超时，请简化问题后重试` | `chat.py:84` | 超 180 s；简化问题或改流式 |

> LLM 侧 **402（额度耗尽）/ 429（限流）** 不会转成 HTTP 错误码，只体现在 `reply` 与 `call_logs.error`，二者处置完全相反 —— 见 [09 §11.1](./09-测试与验收.md)。

## 4. 联调顺序建议

1. **域名白名单**：小程序后台 → 开发设置 → request 合法域名，加 `https://api.tiaowulan.com`
2. **鉴权闭环**（阻塞其余）：宿主后端 `X-API-Key` → `/auth/token` → 拿 `user_id` → 立刻调 `GET /auth/me` 与 `POST /chat` 各一次，确认不 403
3. **跑通 `/chat`**：先用非流式拿到 `session_id`，确认 `reply/ui/data` 结构
4. **渲染卡片**：先做 `text` + `plan_card`，其余按 `/ui-contract` 补齐；缺失一律按 `reply` 文本降级
5. **生图链路**：触发 `image_task` → 2 s 轮询 `/tasks/{id}` → 拼域名展示图片
6. **会话管理**：列表 + 历史 + `session_id` 回传，确认 `ui` 字段解析正确
7. **改流式 `/chat/stream`**：文字追加 + `card` 渲染 + `done` 存 `session_id` + 不支持分块时降级
8. **监控面板**：用 `DASHBOARD_API_KEY` 打开 `https://api.tiaowulan.com/dashboard/`，核对平台/耗时/工具

## 相关文档

- [05-前端对接契约.md](./05-前端对接契约.md) —— 8 种卡片字段级契约、图片前缀、小程序流式实现要点
- [09-测试与验收.md](./09-测试与验收.md) —— 分层测试、验收 Checklist、失败定位手法
- [03-系统架构设计.md](./03-系统架构设计.md) —— 部署形态、ReAct 链路、SSE 实现约束
- [06-数据库设计.md](./06-数据库设计.md) —— `sessions`/`messages`/`image_tasks`/`call_logs` 表结构
- [10-安全与合规.md](./10-安全与合规.md) —— 密钥边界、SSRF、只读约束
- [小程序接入待办清单.md](./小程序接入待办清单.md) —— 前端待办与验收项（业务视角）
