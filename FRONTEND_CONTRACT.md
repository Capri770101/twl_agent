# 🔌 前端对接契约（Frontend Contract）

> ⚠️ **接入必读 —— 请先看这里**
>
> 本智能体是**纯后端 API**，只产出结构化的 `ui / data / action`。**所有 UI 渲染、按钮交互、页面跳转都由宿主平台的前端负责**。
>
> 因此，接入平台**必须先实现下列前端组件**，否则对话会出现「智能体返回了方案数据、前端却没地方展示」的断层：
>
> | UI 组件 | 触发场景 |
> |---|---|
> | `plan_card` 方案卡片 | 推荐现成方案 / DIY 方案定稿 |
> | `shop_card` 店铺卡片 | 推荐可配送、有花材库存的店铺 |
> | `order_card` 订单确认卡 | 用户确认方案+店铺后创建订单 |
> | `pay_jump` 支付跳转 | 订单已创建，需要打开支付页 |
> | `image_task` 生图进度 | 生成效果图（需轮询任务） |
> | `dialog_options` 选项按钮 | 让用户在几个选项里选择 |
> | `text` 文本气泡 | 知识问答 / 兜底文本（最简单，必须有） |
>
> 如果某个组件暂未开发，**不要把对话直接报错中断**：至少能渲染 `reply` 文本 + 把 `data` 原样展示，并按 `action.fallback` 提示用户。详见下方各类型契约。

---

## 一、数据如何到达前端

### 同步 `/chat`

生产请求先通过 `/auth/anonymous` 或 `/auth/wx-login` 获取 token，并在调用 `/chat`、会话和任务接口时携带 `Authorization: Bearer <access_token>`。MCP 通道不渲染本文件中的卡片，只应转换为文本、图片 URL 和平台链接；MCP bridge 只能导出显式安全白名单工具，不能全量暴露工具注册表。

`POST /chat` 返回统一的 `ChatResponse`，前端按 `ui` 字段选择组件、把 `data` 传给该组件渲染：

```json
{
  "reply": "我为你推荐了一个生日花束方案",
  "ui": "plan_card",
  "data": { "plans": [ { "plan_id": "P001", "name": "生日玫瑰花束", "price": 199 } ] },
  "action": {
    "type": "show_plan",
    "payload": { "reply": "...", "ui": "plan_card", "data": { "plans": [] }, "stage": "plan_confirm" },
    "required_capabilities": ["show_plan_page"],
    "fallback": "当前平台暂不支持方案页面，请先展示文本和方案数据。"
  },
  "tool_calls": [],
  "session_id": "conv_xxxx",
  "stage": "plan_confirm"
}
```

### 流式 `/chat/stream`

SSE 事件流。文本按 `thinking` / `text` 事件推流；当出现结构化卡片时，服务端额外推送一条 `card` 事件：

```
event: card
data: {"ui": "plan_card", "data": {"plans": [...]}}
```

收到 `card` 事件后，**前端用它渲染卡片，而不是再去解析 `text` 里的文字**。

### 字段约定

| 字段 | 说明 |
|---|---|
| `reply` | 必渲染的中文说明。即使组件缺失，也要把这段文字展示出来 |
| `ui` | 本次响应的 UI 类型（枚举值见下方清单） |
| `data` | 传给组件的结构化数据，结构与 `ui` 一一对应 |
| `action.type` | 期望平台执行的下一步动作（平台中立，不绑定 SDK） |
| `action.required_capabilities` | 平台「应当具备」的前端/业务能力清单 |
| `action.fallback` | 能力缺失时的降级文案，不能把错误直接抛给用户 |
| `stage` | 会话进度，便于前端展示流程到哪一步 |

> 前端以 `ui + action.required_capabilities` 作为能力对照依据：**只要这个字段出现过而你还没实现，就说明有前端缺口需要补齐。**

---

## 二、UI 类型契约

> 各 `data` 的结构以 `agent/engine/ui_protocol.py` 中的模型为准，运行时可能附带额外字段（如 `tags`、`design`），**前端只消费已知字段，多余字段原样透传、忽略即可**。
> 接入方也可随时调用 `GET /ui-contract` 拉取机器可读的最新清单，避免字段漂移。

### 1. `text` —— 文本气泡（兜底组件）

- **何时出现**：纯知识问答、需求澄清、降级兜底。
- **渲染要求**：普通聊天气泡，渲染 `reply` 即可。`data` 通常为空对象。

```json
{
  "reply": "红玫瑰花语是热情与爱情，适合表白、情人节使用。",
  "ui": "text",
  "data": {}
}
```

### 2. `dialog_options` —— 选项按钮

- **何时出现**：需要用户在多个选项中做出选择（模式选择、确认/修改）。
- **渲染要求**：渲染 `reply` + 一组按钮/选项；用户点选后把 `value` 作为下一条消息发送给 `/chat`。
- **对应能力**：`show_options`

```json
{
  "reply": "你想要哪种方案？",
  "ui": "dialog_options",
  "data": {
    "options": [
      { "label": "现货花束（约200元）", "value": "existing" },
      { "label": "DIY 定制（按你喜好）", "value": "diy" }
    ]
  }
}
```

`options[]` 每项：`label`（展示文案）、`value`（发送回对话的值）。

### 3. `plan_card` —— 方案卡片

- **何时出现**：推荐现成方案、DIY 方案设计/改版完成。
- **渲染要求**：卡片列表，至少展示名称、价格、描述、效果图；提供「确认」「修改/换一款」「去生成效果图」等按钮，并把对应指令发回 `/chat`。
- **对应能力**：`show_plan_page`

```json
{
  "reply": "为你推荐 3 个生日花束方案：",
  "ui": "plan_card",
  "data": {
    "plans": [
      {
        "plan_id": "P001",
        "name": "生日玫瑰花束",
        "price": 199.0,
        "desc": "红玫瑰 11 支 + 满天星，热烈浪漫，适合生日祝福。",
        "effect_image_url": "https://.../rose.png",
        "merchant_name": "向阳花艺",
        "tags": ["生日", "浪漫"],
        "style": "现代"
      }
    ]
  }
}
```

`plans[]` 每项的标准字段（对应 `ui_protocol.PlanCard`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `plan_id` | string | 方案 ID（确认后回传给后端） |
| `name` | string | 方案名称 |
| `price` | number | 价格（元） |
| `desc` | string | 描述 |
| `effect_image_url` | string | 效果图 URL（可为空） |
| `merchant_name` | string | 商家名称（可为空） |

### 4. `shop_card` —— 店铺卡片

- **何时出现**：推荐可配送、花材/库存匹配的店铺（用户有位置时按距离排序）。
- **渲染要求**：店铺列表，展示名称、距离、价格区间、评分；提供「选这家」「去下单」等按钮。
- **对应能力**：`show_shop_page`

```json
{
  "reply": "以下 3 家店可配送且备货充足：",
  "ui": "shop_card",
  "data": {
    "shops": [
      {
        "shop_id": "S001",
        "name": "向阳花艺（五道口店）",
        "distance_km": 1.2,
        "price_range": "中高端",
        "rating": 4.8,
        "delivery": "1小时内送达"
      }
    ]
  }
}
```

`shops[]` 每项标准字段（对应 `ui_protocol.ShopCard`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `shop_id` | string | 店铺 ID（下单时回传） |
| `name` | string | 店铺名称 |
| `distance_km` | number | 距离（公里） |
| `price_range` | string | 价格区间描述（可为空） |
| `rating` | number | 评分（0~5，可为空） |

### 5. `order_card` —— 订单确认卡

- **何时出现**：用户在确认方案+店铺后，智能体已调用下单逻辑创建订单（写入平台订单服务）。
- **渲染要求**：展示订单 ID、明细条目（名称、单价、数量、小计）、合计金额、优惠、方案类型；提供「确认支付」「重新选择」按钮。
- **对应能力**：`create_order`（对接平台订单服务，并非让前端直接调用支付）
- ⚠️ **订单写入、幂等、金额一致性由后端业务保证；前端只做展示与用户确认交互。**

```json
{
  "reply": "订单已为你创建，请确认：",
  "ui": "order_card",
  "data": {
    "order_id": "ORD_20260902_0001",
    "plan_name": "生日玫瑰花束",
    "items": [
      {
        "plan_id": "P001",
        "name": "红玫瑰",
        "role": "主花",
        "unit_price": 12.0,
        "qty": 11,
        "price": 132.0,
        "image": "https://.../rose.png"
      }
    ],
    "total_price": 199.0,
    "discount": 0,
    "plan_type": "existing",
    "effect_image_url": "https://.../rose.png"
  }
}
```

`data` 顶层字段（`order_id`、`items[]`、`total_price`、`plan_type` 对应 `ui_protocol.OrderCard`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `order_id` | string | 订单 ID |
| `items[]` | array | 明细条目（标准字段：`name`/`qty`/`unit_price`/`price`/`image`） |
| `total_price` | number | 合计金额 |
| `discount` | number | 优惠金额 |
| `plan_type` | string | `existing`（现成方案）或 `diy`（定制方案） |
| `effect_image_url` | string | 方案效果图（可为空） |

### 6. `pay_jump` —— 支付跳转

- **何时出现**：订单创建完成，需要引导用户去支付。
- **渲染要求**：给用户明确的「去支付」入口；点击后用 `data` 里的信息**打开宿主平台自己的收银/支付页面**。小程序场景直接 `wx.navigateTo({ url: page_path + 参数化 query })`。
- **对应能力**：`open_payment`（使用平台自身的支付 SDK / 收银台，**智能体不接触支付密钥**）

```json
{
  "reply": "订单已生成，请点击去支付完成付款：",
  "ui": "pay_jump",
  "data": {
    "order_id": "ORD_20260902_0001",
    "page_path": "/pages/order/confirm",
    "params": {
      "order_id": "ORD_20260902_0001",
      "total_price": 199.0,
      "shop_id": "S001"
    },
    "total_price": 199.0,
    "pay_amount": 199.0
  }
}
```

`data` 标准字段（对应 `ui_protocol.PayJump`）：`order_id`、`page_path`（默认 `/pages/order/confirm`）、`params`（跳转携带参数）；运行时可能附带 `total_price` / `pay_amount` 等只读金额字段便于前端展示。

### 7. `image_task` —— 生图任务（进度/结果）

- **何时出现**：用户要求生成效果图。生图是**异步任务**，不能把任务 ID 当图片 URL。
- **渲染要求**：展示「生成中」状态并按 `poll` 轮询结果；成功后展示图片、提供「用这张图」「重新生成」等操作。
- **对应能力**：`start_image_task`（轮询由前端/宿主平台完成）

```json
{
  "reply": "正在为你生成效果图，大约需要 30 秒：",
  "ui": "image_task",
  "data": {
    "task_id": "task_img_0001",
    "poll": "/tasks/task_img_0001",
    "result_url": ""
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 任务 ID |
| `poll` | string | 轮询路径（相对地址，最终结果见下） |
| `result_url` | string | 任务**已完成**时直接给图片 URL；为空表示仍在生成 |

轮询 `GET {poll}`（即 `GET /tasks/{task_id}`）：

```json
{
  "task_id": "task_img_0001",
  "status": "done",
  "result_url": "https://.../effect.png",
  "created_at": "2026-09-02T12:00:00",
  "updated_at": "2026-09-02T12:00:30"
}
```

`status`：`processing`（生成中）/ `done`（成功，带 `result_url`）/ `failed`（失败，带 `error`）。任务状态由服务端数据库持久化；服务重启时遗留的 `processing` 任务会变成 `failed`，前端不要无限轮询。部署方如果没有 `image_tasks` 表，请先执行 [`migrations/001_image_tasks.sql`](migrations/001_image_tasks.sql)。注意：接口实际返回状态值是 `done`，不是 `completed`。

---

## 三、action 与 required_capabilities 对照表

| UI 类型 | action.type | required_capabilities | 平台侧职责 |
|---|---|---|---|
| `text` | `show_text` | — | 渲染文本气泡 |
| `dialog_options` | `show_options` | `show_options` | 渲染选项按钮，点选值回传 |
| `plan_card` | `show_plan` | `show_plan_page` | 渲染方案卡片，确认/修改指令回传 |
| `shop_card` | `show_shop` | `show_shop_page` | 渲染店铺卡片，「选这家」回传 |
| `order_card` | `create_order` | `create_order` | 展示订单详情；对接平台订单服务 |
| `pay_jump` | `open_payment` | `open_payment` | 用平台支付能力打开收银/支付页 |
| `image_task` | `start_image_task` | `start_image_task` | 展示生成状态 + 轮询 `GET /tasks/{task_id}` |

> `required_capabilities` 只表示**接入方需要具备的前端/业务能力**，不是智能体可自行调用的接口。能力缺失时按 `action.fallback` 或 `reply` 做文本降级，不要中断会话。

---

## 四、接入前 Check 清单（前端自检）

接入 /chat 前，请逐项确认：

- [ ] 能渲染 `text`（最低要求）
- [ ] 实现 `plan_card`：名称/价格/描述/图片 + 确认、修改按钮
- [ ] 实现 `dialog_options`：选项按钮 → 值回传
- [ ] 实现 `shop_card`：名称/距离/评分 + 选择按钮
- [ ] 实现 `order_card`：明细 + 合计 + 确认交互
- [ ] 实现 `pay_jump`：跳转到平台自己的支付页
- [ ] 实现 `image_task`：生成中状态 + `GET /tasks/{id}` 轮询 + 结果图展示
- [ ] `/chat/stream` 收到 `card` 事件时能正确渲染卡片
- [ ] 任一能力缺失时有文本降级（渲染 `reply` + `action.fallback`），不报错中断
- [ ] 可以用 `GET /ui-contract` 拉取清单，与本仓库 `agent/engine/ui_protocol.py` 字段保持一致
- [ ] MCP 通道将卡片降级为文本、图片 URL 和平台链接，不依赖宿主直接渲染 `ui/data`

**对接建议**：先跑通「文本 → plan_card → image_task → shop_card → order_card → pay_jump」全链路，再处理其余细节。

---

## 五、参考

- 协议模型（唯一权威 schema）：`agent/engine/ui_protocol.py`
- 机器可读清单（程序化对照）：`GET /ui-contract`
- 业务链路说明：`README.md` → 「平台接入契约」「完整业务流程」
- 部署与配置：`DEPLOY.md`
