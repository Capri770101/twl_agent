# 花艺智能体 · 接入说明

> **版本**：v2（2026-09，平台只读重构后）。本文档回答一个问题：**把一个"花艺智能体"接进你的业务，到底要做哪些事。**
> 先读 §0 角色地图，找到自己那一列，再翻到对应章节逐条打勾；全部打勾后按 §6 验收。
>
> **一句话架构**：智能体是纯后端（ReAct + 工具编排），**不持有任何商品/店铺/订单数据，也绝不写任何外部库**。
> 商品来自你给的**只读数据库**（实时查，不是镜像），下单走你提供的**平台自有下单 API**（转发，不是直写）。

---

## 0. 角色地图：谁要做什么

| 角色 | 要交付 / 完成的事 | 详见 |
|---|---|---|
| **业务平台方**（有花要卖的一方） | ① 提供平台库**只读连接串**并激活映射，让智能体"看得见"商品/店铺 ② 提供一个**平台自有下单接口**，让智能体"替你下单" | **§2、§3（核心必读）** |
| **前端宿主**（小程序 / H5 / App） | 实现对话 UI 组件、对接鉴权与 /chat 契约 | §4 + FRONTEND_CONTRACT.md |
| **部署方**（运维 / 开发） | 把服务跑起来：模型 Key、内部库、JWT、域名 | §5 + DEPLOY.md |
| 终端用户 | 无——直接用 | — |

**数据流总览**：

```
终端用户 ──> 前端宿主 ──Bearer token──> 智能体 /chat（ReAct 编排）
                                          │
      ┌───────────────────────────────────┼────────────────────────────────┐
      ▼ 只读 SELECT（实时）              ▼ POST 转发（下单）              ▼ 智能体自身 PG
 平台商品/店铺/订单库               平台自有下单 API              会话/消息/记忆/DIY/映射/生图任务
 PLATFORM_DB_<SOURCE_ID>_URL      PLATFORM_ORDER_API_URL        DATABASE_URL（接入方无需关心）
 需先激活 active mapping          需平台方实现并托管             只存智能体自身运行数据
```

---

## 1. 架构契约（动手前先理解，避免踩旧文档的坑）

1. **智能体"零库存"**：本地不再有任何商品/店铺/订单镜像表（plans / shops / orders 等已删除），商品查询也不落任何缓存。
2. **读商品 = 实时查你的库**：通过只读连接 `PLATFORM_DB_<SOURCE_ID>_URL` 查询，且每个数据源必须先在智能体内**激活一条映射（mapping）**，告诉它"你的表/字段 ↔ 标准实体字段"如何对应。没有映射就查不了——这是**故意**的安全设计，防止 AI 乱猜表结构。
3. **下单 = 转发你的 API**：`create_order` 只做组装，把订单 JSON 转发到 `PLATFORM_ORDER_API_URL`。**绝不直写你的库，也绝不写自己的库**。接口未配置时下单会**明确报错**并引导用户去平台下单——不会静默产生假订单。
4. **双库严格分离**：你的业务库只读被查；智能体内部 PG 只存会话/记忆/DIY 方案/映射/任务等自身数据，接入方无需关心也不需要权限。
5. **能力边界**：智能体负责"理解需求 → 推方案 → DIY → 出效果图/贺卡 → 申请下单（到支付前一步）"；**支付与最终成交永远在你平台闭环**（pay_jump 跳回你平台的收银台）。
6. **过期名词提示**：旧版本文档里的 `search_plans` / `search_shops` / `db_auto_*` / `match_shop_items` / `db_discover` / `source_inspect` / `data_mapping.json` / `write_enabled=true` / `shop_tools.py` 均已废弃删除。**凡出现这些词的内容都是旧版**，以本文档与源码为准。

---

## 2. 业务平台方 · 待办 A：让智能体"看得见"你的商品

### A1. 准备一个只读数据库账号

- 平台库需为 **PostgreSQL 兼容**（当前只实现了 PG 协议连接器）。
- 开一个**最小权限只读账号**：仅 `SELECT`，例如：
  ```sql
  CREATE USER flora_read WITH PASSWORD '换一个强密码';
  GRANT CONNECT ON DATABASE your_db TO flora_read;
  GRANT USAGE ON SCHEMA public TO flora_read;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO flora_read;
  ```
- 网络可达：你的库若不对公网开放，把智能体部署服务器的出口 IP 加入白名单。

### A2. 确认库里有这些"实体"

智能体按 **entity** 理解你的数据。字段名不一样没关系（映射时指过去即可），但语义上要有：

| entity | 含义 | 至少需要这些语义的字段 |
|---|---|---|
| `plan` | 可售花束 / 方案 / 商品 | id、名称、价格、描述、图片、标签或分类 |
| `shop` | 可配送店铺 | id、名称、地址、评分、配送信息 |
| `order` | 订单（查单用） | id、用户id、方案id、金额、状态、创建时间 |
| `user` | 用户（可选） | id、昵称等 |

### A3. 配置连接串（服务端环境变量）

```env
PLATFORM_DB_MAIN_URL=postgresql://flora_read:密码@你的库地址:5432/你的库名
```

- `SOURCE_ID` 用英文小写标识，一个平台一个（`main` / `mall` / `partner`…），工具参数里的 `source_id` 与之一致。
- 连接串只存在于服务端环境（.env / docker env_file），**LLM 永远看不到**。

### A4. 激活映射（每个 source 一次）

映射流程（建议直接与智能体对话让它执行，或由有权限的开发按序调用以下工具）：

```text
platform_db_test_connection(source_id)     ① 连通性
platform_db_discover(source_id, sample_rows=0)   ② 获取库结构概况（不读业务样本）
platform_db_sample_table(...)（可选）       ③ 字段语义不明时看 ≤5 行脱敏样本
platform_mapping_draft(profile=②的输出)     ④ 生成映射草案（含候选/置信度/证据，不直接落库）
platform_mapping_save_draft(...)            ⑤ 存为带版本的草案
platform_mapping_set_status(id,"approved")  ⑥ 人工审核通过
platform_mapping_set_status(id,"active")    ⑦ 激活（同 source 同时仅一条 active，新激活自动撤销旧映射）
```

状态流转：`draft → reviewed → approved → active`（可 `revoked`）。

### A5. 验证

- 对话里说「推荐一束送妈妈的玫瑰」→ 应返回**真实商品**的方案卡片（数据来自你的库）。
- 若报 **"没有该来源的 active mapping"** → 回 A4 激活；若报 **"active mapping 对 entity 无可用映射"** → 该实体在 A4 的映射里没配可用表/字段，回 A2/A4。
- 映射是查询的**前置条件**；没激活前智能体只会如实说"平台商品暂未接入"，不会编造方案。

---

## 3. 业务平台方 · 待办 B：让智能体"替你下单"

智能体绝不写你的库——它把订单**转发**给你实现的 HTTP 接口，由你的接口完成落库、扣库存、返回收银台信息。

### B1. 实现一个"平台自有下单接口"

- `POST <你的 URL>`，`Content-Type: application/json`，建议你的网关限流并记录审计。
- 鉴权：智能体侧会带 `Authorization: Bearer <PLATFORM_ORDER_API_KEY>`（若配置了 KEY）；你的接口校验该 Header，或按你们自己的签名约定校验。

### B2. 智能体发给你的请求体（字段固定，可按需增加）

```json
{
  "request_id": "req_xxx（幂等键，请用它去重，防重复下单）",
  "channel": "flora_agent",
  "external_user_id": "你们体系内的用户ID（与 /auth/token 提交的一致）",
  "agent_session_id": "会话ID",
  "shop_id": "平台真实店铺ID（来自 shop 实体）",
  "plan": {
    "plan_id": "方案/商品ID",
    "name": "方案名",
    "type": "existing | diy",
    "price": 198.0,
    "desc": "方案描述（≤500字）",
    "image": "方案图URL",
    "recipient": "收花人",
    "occasion": "场合",
    "card_message": "贺卡文案（若有）"
  },
  "items": [
    {"kind": "flower", "name": "粉玫瑰", "qty": 11},
    {"kind": "design_note", "text": "设计说明"}
  ],
  "estimated_total": 198.0,
  "remark": "来自花卉 DIY 智能体的订单请求"
}
```

> `items` 与 `estimated_total` 仅供你方参考核算；**金额、库存、可用性一律以你平台为准**，你的接口应自行校验并返回最终价。

### B3. 你要返回的响应（智能体按这些字段解析）

```json
{
  "order_id": "你平台的订单号",
  "status": "created",
  "total_price": 198.0,
  "pay": {
    "type": "miniapp",
    "page_path": "/pages/order/pay",
    "params": { "order_no": "你平台的订单号" }
  }
}
```

兼容别名（你的接口用这些也行，解析层会兼容）：
- `order_id` ⇄ `order_no` / `id` / `trade_id`
- `total_price` ⇄ `total` / `amount` / `pay_amount`
- 支付信息：`pay` 对象 或直接给顶层 `pay_url`
- 业务失败：返回 `{ "error": "原因" }` 或 `{ "code": 非0, "message": "原因" }`

智能体拿到响应后会把 `pay` 整理成 **pay_jump** 交回前端，由前端打开你平台的收银台完成支付——支付回调由你们自己处理。

### B4. 配置（服务端环境变量）

```env
PLATFORM_ORDER_API_URL=https://你的平台域名/api/order/create
PLATFORM_ORDER_API_KEY=可选；配置后智能体以 Authorization: Bearer <key> 调用
```

### B5. 验证与"未配置"行为

- 验证：完整走「挑花 → 选店 → 确认下单」→ 返回 `order_card` + `pay_jump`（含你平台的 `page_path`）。
- **未配置 `PLATFORM_ORDER_API_URL` 时**：`create_order` 明确报错「平台下单接口未配置…请引导用户前往平台/店铺完成下单」，**不会**本地落单、**不会**静默成功——前端看到此类报错请如实展示。

---

## 4. 前端宿主 · 待办：鉴权 + 对话 + UI 组件

> 详细数据契约以 **[FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)** 与 `GET /ui-contract` 为准，本节是接入动作清单。

### 4.1 鉴权（两级，容易踩坑）

1. **你们后端**（服务端，勿下发客户端）：
   ```bash
   curl -X POST https://<智能体域名>/auth/token \
     -H "X-API-Key: <平台密钥>" -H "Content-Type: application/json" \
     -d '{"external_user_id": "你们体系内的用户ID"}'
   # → { access_token, user_id: "wxmini_xxx（派生值）", ... }
   ```
2. **前端**带 `Authorization: Bearer <access_token>` 调 `/chat`；**请求体里的 `user_id` 必须等于换 token 返回的那个派生值**（平台id + 你们用户id 哈希派生，各平台隔离）——自己另填会 **403**。

要点：平台密钥是服务端机密；token 有效期 30 天可缓存按用户复用；小程序也可走 `POST /auth/wx-login`（`{"code": ...}`，需配 `WECHAT_APPID`/`WECHAT_SECRET`）；匿名登录生产环境默认关闭。

### 4.2 对话接口

| 接口 | 用途 |
|---|---|
| `POST /chat` | 同步对话：`message` / `user_id` / `session_id?` / `location?` / `shop_id?` |
| `POST /chat/stream` | SSE 流式（事件：`tool_call` / `text` 增量需拼接 / `card` / `done` / `error`） |
| `GET /tasks/{task_id}` | 异步生图任务轮询（`status`: `processing`/`done`/`failed`） |
| `GET /ui-contract` | 机器可读 UI 契约清单（建议对接脚本自动拉取对照） |
| `GET /health` | 健康检查 |

同步响应关键字段：`reply`（文字，永远有）+ `ui`（渲染类型）+ `data`（结构化数据）+ `action`（`{type, required_capabilities, fallback}`）+ `session_id`（下轮带回）+ `stage`（进度焦点）。

### 4.3 需要实现的 UI 组件（接入前 Check 清单）

| 组件 | 何时出现 | 缺失时 |
|---|---|---|
| `text` 文本气泡 | 知识问答 / 兜底 | **最低要求，必须有** |
| `dialog_options` 选项按钮 | 让用户在几个选项里点选 | 渲染 `action.fallback` 文本 |
| `plan_card` 方案卡片 | 推荐现成方案 / DIY 定稿 | fallback 文本 |
| `shop_card` 店铺卡片 | 推荐可配送店铺 | fallback 文本 |
| `order_card` 订单确认卡 | 下单成功 | fallback 文本 |
| `pay_jump` 支付跳转 | 跳你平台的收银台 | fallback 文本 |
| `image_task` 生图进度 | 效果图（**异步**，需轮询） | fallback 文本 |
| `greeting_card` 电子贺卡 | 下单后引导 / 主动要（**同步**出图） | fallback 文本 |

`greeting_card` 数据形如：`{image_url, text, recipient, sender, template, note}`——`image_url` 直接可展示（`/generated/greet_*.png`），无需轮询。

### 4.4 约定与兼容性承诺

1. 后端 `ui` 枚举**只增不改**；前端遇到不认识的 `ui` 一律降级为渲染 `reply` + `action.fallback`，**不报错、不白屏**。
2. 生图任务 `status` 实际取值为 `done`（不是 `completed`）；`processing` 任务服务重启后会变 `failed`，前端不要无限轮询。
3. `shop_id` 传了即锁定店铺（从某店铺页进入导购）；不传则智能体自由推荐。商品/方案/店铺的业务 ID 由智能体在 `data` 里回传，前端只需要"把用户的选择作为下一条消息回传"。

### 4.5 常见错误码

| 状态码 | 含义 | 处理 |
|---|---|---|
| 401 | token 缺失/无效 | 重新走 `/auth/token` |
| 403 | `user_id` 与 token 不匹配 | **必须用 `/auth/token` 返回的 `user_id`** |
| 403 | 匿名登录被禁 | 生产用 `/auth/token` 或 `/auth/wx-login` |
| 500 | 智能体执行失败 | 重试；持续出现联系部署方 |
| 504 | 超时（>180s） | 简化问题或用 `/chat/stream` |

---

## 5. 部署方 · 待办：把服务跑起来（摘要）

完整配置见 **[DEPLOY.md](DEPLOY.md)**，这里只列"必须做的事"：

1. **模型服务**（必填）：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`（OpenAI 兼容，或 hy 大模型一组 `HY_*`）。模型需支持 function/tool calling。
2. **内部库**（必填，生产禁 SQLite）：`DATABASE_URL` 指向智能体自己的 PostgreSQL；无 DDL 权限先执行 [`migrations/001_image_tasks.sql`](migrations/001_image_tasks.sql)。
3. **安全**（必填）：`JWT_SECRET` ≥32 位；`PLATFORM_API_KEYS` 给每个前端宿主平台签发 `platform_id=key`；`ALLOWED_ORIGINS` 明确前端域名；HTTPS 域名 + 反向代理（SSE 需关 proxy_buffering）。
4. **出图**（按需）：效果图 `IMAGE_PROVIDER=mock|hy`；**电子贺卡为本地模板合成**（Pillow），依赖中文字体——Docker 镜像已内置 `fonts-noto-cjk`，裸机部署需自装中文字体（可用 `CARD_FONT_PATH` 指定）。
5. **平台数据**：按 §2、§3 把 `PLATFORM_DB_*_URL` 与 `PLATFORM_ORDER_API_URL` 注入环境。

---

## 6. 验收 Checklist

### 部署方
- [ ] `GET /health` 返回 `{"status":"ok",...}`
- [ ] `GET /ui-contract` 返回含 `greeting_card` 在内的 8 类组件清单
- [ ] 无 DDL 权限库已执行 `migrations/001_image_tasks.sql`，启动日志通过自检

### 业务平台方（数据）
- [ ] 只读账号开通，`PLATFORM_DB_<SOURCE_ID>_URL` 已配置
- [ ] `platform_db_test_connection` 通过
- [ ] 对话内已完成映射并激活（`platform_mapping_get_active` 有结果）
- [ ] 对话「推荐一束送妈妈的玫瑰」返回**真实商品** plan_card，无"active mapping"报错

### 业务平台方（下单）
- [ ] 下单接口已实现并按 §3.2/§3.3 收发 JSON
- [ ] `PLATFORM_ORDER_API_URL` 已配置
- [ ] 完整走通「挑花 → 选店 → 确认」返回 `order_card` + `pay_jump`，`page_path` 能打开你平台收银台
- [ ] 支付回调在你们平台闭环，智能体侧无任何订单落库（可查智能体内部库确认无 orders 表）

### 前端宿主
- [ ] token 换取与 `user_id` 回传正确（无 403）
- [ ] `text` / `plan_card` / `shop_card` / `order_card` / `pay_jump` / `image_task` / `greeting_card` 均已实现（或按 fallback 降级不中断）
- [ ] 未知 `ui` 值降级正常、SSE `text` 增量正确拼接、生图任务轮询到 `done`

---

## 7. 文档地图与支持

| 文档 | 内容 | 谁看 |
|---|---|---|
| **本文档** | 接入动作清单（按角色） | 所有接入方，先读 |
| [FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md) | 每个 UI 组件的字段/渲染要求/示例/Check 清单 | 前端开发者 |
| [DEPLOY.md](DEPLOY.md) | 环境变量、迁移、Nginx、裸机部署 | 部署方 |
| [README.md](README.md) | 项目总览、架构、开发指引 | 开发者 |

- 接口契约以 `GET /ui-contract` 与源码为准；线上服务版本见 `GET /health` 的 `version`。
- 对接问题请携带 `session_id` + 时间戳反馈，便于定位。
