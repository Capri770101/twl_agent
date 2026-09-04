# 花艺智能体 API 接入说明

面向接入方（小程序 / H5 / App 后端）的接口对接文档。所有内容均以线上实际行为与源码为准。

> 后端只产出**结构化数据**（`ui` / `data` / `action`），**前端渲染由接入方自行实现**。
> 接入前请先对照第 8 节确认自己已具备相应的 UI 组件，否则会出现"有数据但没地方展示"。

---

## 1. 智能体实际部署地址

| 项目 | 值 |
|---|---|
| **生产地址** | `https://api.tiaowulan.com` |
| 协议 | 仅 HTTPS（TLS 1.2 / 1.3），HTTP 会 301 跳转 |
| 证书到期 | 2026-12-01（到期前需更换，届时会提前通知） |
| 健康检查 | `GET https://api.tiaowulan.com/health` |
| IP 直连 | **不提供**。后端端口已收口，只走域名 + 443 |

健康检查返回示例：

```json
{"status":"ok","service":"flora-agent","version":"1.0.0","env":"prod"}
```

---

## 2. 聊天接口路径

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/chat` | 对话（同步，等全部处理完一次性返回） |
| `POST` | `/chat/stream` | 对话（SSE 流式，逐段返回） |
| `POST` | `/chat/reset` | 清空会话 |
| `GET` | `/conversations` | 会话列表 |
| `GET` | `/conversations/{id}/messages` | 会话历史消息 |
| `POST` | `/conversations` | 新建会话 |
| `GET` | `/tasks/{task_id}` | 生图任务状态轮询 |
| `GET` | `/ui-contract` | 拉取全量 UI 组件契约（机器可读，建议对接时先看这个） |
| `GET` | `/health` | 健康检查 |

---

## 3. 请求方式与参数格式

**请求方式**：`POST` + `Content-Type: application/json`

**请求头**：

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**请求体**（`ChatRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `message` | string | ✅ | 用户输入的这句话 |
| `user_id` | string | ✅ | **必须等于 `/auth/token` 返回的 `user_id`**，否则 403 |
| `session_id` | string | ❌ | 会话 ID。**不传则自动新建会话**；续聊时把上一次返回的 `session_id` 带回来 |
| `location` | object | ❌ | 位置信息（用于按距离推荐店铺），如 `{"lat":39.9,"lng":116.4}` |
| `shop_id` | string | ❌ | 锁定店铺（见第 6 节） |

**示例**：

```bash
curl -X POST https://api.tiaowulan.com/chat \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "wxmini_55f93251cef9d92c06cd0e5e",
    "message": "推荐一束送妈妈的生日花，预算300",
    "session_id": "e8fc461f93354ac68c264ffaf47d27b6"
  }'
```

**超时**：服务端单次请求超时 180 秒（超时返回 504）。建议客户端超时设为 190 秒以上，或直接使用流式接口。

---

## 4. 返回格式与流式输出

### 4.1 同步接口 `POST /chat`

返回 `ChatResponse` 对象：

| 字段 | 类型 | 说明 |
|---|---|---|
| `user_id` | string | 回显用户标识 |
| `reply` | string | **给用户的文字回复**（始终有值，可直接当文本气泡渲染） |
| `ui` | string | UI 类型枚举，决定 `data` 怎么渲染（见下表） |
| `data` | object | 对应 `ui` 类型的结构化数据 |
| `action` | object | 下一步动作契约：`{type, payload, required_capabilities, fallback}` |
| `tool_calls` | array | 本轮智能体实际调用的工具记录 `[{name, arguments, result, status}]`，用于调试/审计 |
| `session_id` | string | **会话 ID，下一轮要带回来** |
| `stage` | string | 当前进度焦点，如 `analyze` / `diy_design` / `shop_recommend` / `order_confirm` / `done` |

**`ui` 枚举与 `data` 结构**：

| `ui` 值 | 含义 | `data` 结构 |
|---|---|---|
| `text` | 纯文本 | `{}`（渲染 `reply`） |
| `dialog_options` | 选项按钮 | `{options:[{label, value}]}` — 用户点击后把 `value` 作为下一条 `message` 回传 |
| `plan_card` | 方案卡片 | `{plans:[{plan_id, name, price, desc, effect_image_url, merchant_name}]}` |
| `shop_card` | 店铺卡片 | `{shops:[{shop_id, name, distance_km, price_range, rating}]}` |
| `order_card` | 订单确认卡 | `{order_id, items:[...], total_price, plan_type}` |
| `pay_jump` | 支付跳转 | `{order_id, page_path, params}` — **智能体不接触支付**，用这个打开你们自己的收银台 |
| `image_task` | 生图任务 | `{task_id, poll, result_url}` — 按 `poll` 轮询 `GET /tasks/{task_id}` 拿结果 |

`action` 是给程序消费的：`type` 为动作类型（`show_text`/`show_plan`/`create_order`/`open_payment`/…），`required_capabilities` 列出前端需要具备的能力，**`fallback` 是能力缺失时的降级文案**——前端没实现某个组件时，请渲染 `fallback` 而不是报错中断。

**完整示例**：

```json
{
  "user_id": "wxmini_55f93251cef9d92c06cd0e5e",
  "reply": "为您设计了「温柔告白」花束，预算约 300 元。",
  "ui": "plan_card",
  "data": {
    "plans": [
      {"plan_id":"P001","name":"温柔告白","price":299.0,
       "desc":"粉玫瑰+满天星","effect_image_url":"","merchant_name":"向阳花艺"}
    ]
  },
  "action": {
    "type": "show_plan",
    "payload": {},
    "required_capabilities": ["show_plan_page"],
    "fallback": "为你设计了一束粉玫瑰花束，约 299 元。"
  },
  "tool_calls": [
    {"name":"search_plans","arguments":{"keyword":"母亲"},"result":"...","status":"ok"}
  ],
  "session_id": "e8fc461f93354ac68c264ffaf47d27b6",
  "stage": "diy_design"
}
```

### 4.2 流式接口 `POST /chat/stream`（**支持**）

SSE（Server-Sent Events），请求体与 `/chat` 完全相同，响应 `Content-Type: text/event-stream`。

**事件类型（以线上实测为准）**：

| 事件 | 数据 | 说明 |
|---|---|---|
| `tool_call` | `{"name":"retrieve_knowledge","status":"ok"}` | 智能体正在调用工具（可用来显示"思考中"） |
| `text` | `{"content":"玫瑰的花语是…"}` | **增量文本片段**，需按顺序拼接 |
| `card` | 卡片数据 | 结构化卡片（与 `ui`/`data` 对应） |
| `done` | `{"session_id":"..."}` | 流结束，**记得保存 session_id** |
| `error` | `{"message":"..."}` | 出错 |

实测样例：

```
event: tool_call
data: {"name": "retrieve_knowledge", "status": "ok"}

event: text
data: {"content": "玫瑰的花语是爱情、热烈、浪漫、尊敬"}

event: text
data: {"content": "。"}

event: done
data: {"session_id": "e8fc461f93354ac68c264ffaf47d27b6"}
```

**注意**：`text` 是**增量片段**（实测会把一句完整的话切成多段，甚至单独一个句号），前端必须**累加拼接**后展示，不要把每段当成独立消息。

---

## 5. 鉴权方式 / API Key

采用**两级认证**：平台密钥换 token，token 调业务接口。

### 第一步：后端换 token（在你们的服务端做）

```bash
curl -X POST https://api.tiaowulan.com/auth/token \
  -H "X-API-Key: <平台密钥>" \
  -H "Content-Type: application/json" \
  -d '{"external_user_id": "你们体系内的用户ID"}'
```

返回：

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "user_id": "wxmini_55f93251cef9d92c06cd0e5e",
  "platform_id": "wxmini"
}
```

### 第二步：用 token 调业务接口

```
Authorization: Bearer <access_token>
```

### 关键约定（容易踩坑）

1. **`user_id` 必须用换 token 时返回的那个派生值**（形如 `wxmini_xxx`）。
   它由 `平台ID + 你们的用户ID` 哈希派生，各平台天然隔离。**自己另填一个会直接 403。**
2. **`external_user_id` 是你们体系内的真实用户标识**，后端不做二次校验，信任由接入方保证。
3. **平台密钥（`X-API-Key`）属于服务端机密，绝对不要下发到客户端/小程序前端**，只能在你们后端调用。
4. token 有效期 **720 小时（30 天）**，过期重新换取即可（不需要用户重新登录）。
5. 建议做法：在你们后端缓存 token（按用户维度），不要每次对话都换。

### 其他登录方式

- 微信小程序也可直接用 `POST /auth/wx-login`（body `{"code":"..."}`，微信 `jscode2session` 的 code），由智能体侧直连微信换 token。
- **匿名登录在生产环境已关闭**，仅开发联调可用。

---

## 6. 是否需要传 shopId、商品 ID、门店资料？

**大部分都不需要。接入方只需传 `user_id` + `message`，业务 ID 由智能体内部流转。**

| 项目 | 是否要传 | 说明 |
|---|---|---|
| `shop_id` | **可选** | 传入后**锁定店铺**：整个会话只搜索/推荐该店铺的商品。适用于"从某个店铺详情页进入导购"的场景。不传则智能体自由推荐。 |
| 商品 / 方案 ID | **不需要** | 智能体自己调工具搜索商品，通过 `data.plans[].plan_id` 回传给前端。用户选择后，把选项的 `value`（即 plan_id）作为下一轮 `message` 回传即可，或在 `dialog_options` 里直接点选。 |
| 门店资料 / 店铺数据 | **不需要** | 店铺数据由智能体侧维护，接入方无需同步或上传。 |
| 商品目录 | **不需要** | 同上，由智能体侧的数据源提供。 |

**一句话**：前端只需要管"用户说了什么"和"把返回的卡片渲染出来"，中间的选品、比价、推荐逻辑全在智能体侧。

---

## 7. 服务更新后是否保持接口兼容？

**承诺如下**：

1. **只增不改**：`ChatResponse` 的字段**只会新增，不会删除或改变已有字段的含义**。新增字段一律带上默认值，老客户端不感知。
2. **`ui` 枚举可扩展**：未来可能新增 UI 类型。前端遇到**不认识**的 `ui` 值时，请统一降级为：渲染 `reply` 文本 + 若 `action.fallback` 有值则一并展示。**不要报错、不要白屏。**
3. **`action.fallback` 是兼容性保险**：组件没实现时的兜底文案由服务端下发，前端无需硬编码。
4. **不做 URL 版本前缀**：当前接口路径（`/chat`、`/chat/stream` 等）保持稳定，通过 `/health` 返回的 `version` 字段标识服务版本（当前 `1.0.0`）。
5. **破坏性变更**：如确需破坏性调整，会提前通知并提供过渡期，新旧版本并存一段时间。
6. **建议的健壮性做法**：
   - 前端解析 `data` 时做**防御式取值**（字段缺失用默认值，不要直接抛异常）
   - 严格按 `ui` 值分发渲染，未知类型走降级
   - 新 `ui` 类型上线前会先更新 `GET /ui-contract`，可定期拉取比对

---

## 8. 接入前：前端需具备的组件

调用 `GET /ui-contract` 可拉取机器可读的完整契约。需要实现的组件：

| 组件 | 必需性 | 缺失时的处理 |
|---|---|---|
| `text` 文本气泡 | **必需（最低要求）** | — |
| `dialog_options` 选项按钮 | 建议 | 用 `fallback` 纯文本 |
| `plan_card` 方案卡片 | 建议 | 用 `fallback` 纯文本 |
| `shop_card` 店铺卡片 | 建议 | 用 `fallback` 纯文本 |
| `order_card` 订单确认卡 | 建议 | 用 `fallback` 纯文本 |
| `pay_jump` 支付跳转 | 建议 | 用 `fallback` 纯文本 |
| `image_task` 生图进度 | 建议 | 用 `fallback` 纯文本（含轮询 `GET /tasks/{task_id}`） |

**只有 `text` 是硬性要求**——其余组件缺失时，只要正确渲染 `reply` + `fallback`，业务链路依然完整可用。

---

## 9. 最小接入示例（Node.js）

```javascript
const BASE = 'https://api.tiaowulan.com';
const API_KEY = process.env.FLORA_API_KEY; // 服务端保管，切勿下发前端

// 1) 换取 token（建议在服务端按用户缓存，有效期 30 天）
async function getToken(externalUserId) {
  const res = await fetch(`${BASE}/auth/token`, {
    method: 'POST',
    headers: { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({ external_user_id: externalUserId }),
  });
  return res.json(); // { access_token, user_id, ... }
}

// 2) 发起对话
async function chat(token, userId, message, sessionId) {
  const res = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, message, session_id: sessionId }),
  });
  const data = await res.json();
  console.log(data.reply);        // 文字回复
  console.log(data.session_id);   // 下一轮要带回来
  console.log(data.ui, data.data);// 按 ui 渲染 data
  return data;
}
```

---

## 10. 常见错误码

| 状态码 | 含义 | 处理 |
|---|---|---|
| 401 | 缺少或无效的 token | 重新走 `/auth/token` |
| 403 | `user_id` 与 token 不匹配 | **必须用 `/auth/token` 返回的 `user_id`** |
| 403 | 匿名登录被禁用 | 生产环境请使用 `/auth/token` 或 `/auth/wx-login` |
| 500 | 智能体执行失败 | 重试；持续出现请联系我们 |
| 504 | 处理超时（>180s） | 简化问题，或改用 `/chat/stream` |

---

## 联系与变更

- 接口契约以 `GET /ui-contract` 与本文档为准
- 服务版本见 `GET /health` 的 `version` 字段
- 有任何对接问题，请携带 `session_id` + 时间戳反馈，便于定位
