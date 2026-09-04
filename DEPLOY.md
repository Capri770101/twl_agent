# 跳舞兰花卉智能体 —— 部署配置指南

本文档按模块列出所有需要配置的项，覆盖部署方 / 运维 / 数据治理 / 业务平台对接的所有环节。

---

> ⚠️ **前端对接必读（重要）**
>
> 本包是**纯后端 API**，只产出结构化 `ui / data / action`，**前端渲染由宿主平台自行负责**。接入前请确认宿主前端**已经具备**以下组件，否则会出现「智能体返回了方案数据、前端却没地方渲染」的断层：
>
> - `text` 文本气泡（最低要求，必须有）
> - `dialog_options` 选项按钮
> - `plan_card` 方案卡片（展示 + 确认/修改按钮）
> - `shop_card` 店铺卡片（锁定模式下不需要）
> - `order_card` 订单确认卡
> - `pay_jump` 支付跳转（打开平台自己的支付页）
> - `image_task` 生图进度（含 `GET /tasks/{task_id}` 轮询）
> - `greeting_card` 电子贺卡（含 `/generated/{filename}.png` 大图）
>
> 每个 UI 类型的数据结构、渲染要求与示例见 **[FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)**（接入必读）；也可调用 `GET /ui-contract` 拉取机器可读清单程序化对照。能力缺失时请按 `action.fallback` / `reply` 做文本降级，不要报错中断。

> **非 UI 配套也必须满足**：生产环境需要 PostgreSQL + `psycopg[binary]` + 可写的图片目录或对象存储 / CDN。生图 / 贺卡任务状态持久化在 `image_tasks` 表；图片文件默认写入 `data/generated/`。使用 CDN / 对象存储时请配置 `IMAGE_PUBLIC_BASE_URL`，并设置对象存储生命周期清理规则。

> **鉴权必读**：本包提供 `POST /auth/token`（通用平台接入，X-API-Key + `external_user_id` → JWT）/ `POST /auth/anonymous`（开发联调；生产默认关闭）/ `POST /auth/wx-login`（微信 jscode2session）/ `GET /auth/me`。生产环境必须配置至少 32 位 `JWT_SECRET`，服务会强制 Bearer 鉴权；多平台接入建议同时配置 `PLATFORM_API_KEYS`；匿名登录在生产默认关闭。

> **数据库没有 `image_tasks` 表怎么办？** 有建表权限时，服务启动会通过 `CREATE TABLE IF NOT EXISTS` 自动创建；生产数据库通常由 DBA 管理，应用账号没有 DDL 权限时，请先执行 [`migrations/001_image_tasks.sql`](migrations/001_image_tasks.sql)，再启动服务。启动自检失败会直接提示迁移文件路径。

---

## 目录

1. [环境变量（.env）](#1-环境变量env)
2. [数据库接入模型](#2-数据库接入模型)
3. [数据库与生图迁移](#3-数据库与生图迁移)
4. [支持的图像生成提供商](#4-支持的图像生成提供商)
5. [AI 电子贺卡与字体](#5-ai-电子贺卡与字体)
6. [店铺锁定模式](#6-店铺锁定模式)
7. [API 一致性说明](#7-api-一致性说明)
8. [部署检查](#8-部署检查)
9. [API 接口一览](#9-api-接口一览)
10. [多平台接入流程](#10-多平台接入流程)
11. [常见问题](#11-常见问题)
12. [知识库扩充规则](#12-知识库扩充规则)
13. [封装边界说明](#13-封装边界说明)
14. [排障工具箱](#14-排障工具箱)

---

## 1. 环境变量（.env）

复制 `.env.example` 为 `.env`，按以下分段配置。下面是字段清单（与 `.env.example` 一一对应）。

### 1.1 服务与运行

| 变量 | 说明 | 默认 |
|------|------|------|
| `APP_ENV` | `dev` / `prod`；生产强制 Bearer 鉴权 | `prod` |
| `HOST` | 监听地址 | `0.0.0.0` |
| `PORT` | 监听端口 | `8000` |

### 1.2 数据库（生产必填 PostgreSQL）

| 变量 | 说明 |
|------|------|
| `DATABASE_URL` | 智能体内部 PostgreSQL 连接串。方式一（docker-compose 内置）：`postgresql://flora:密码@postgres:5432/flora_agent`；方式二（外部 PG）：写实际地址 |
| `POSTGRES_PASSWORD` | docker-compose 内 postgres 容器密码，外部 PG 时可留空 |

> 生产 / 多实例必须使用 PostgreSQL，**禁止 SQLite**。

### 1.3 JWT / 多平台 Key / 鉴权

| 变量 | 说明 |
|------|------|
| `JWT_SECRET` | JWT 签名密钥，**至少 32 位**（如 `python -c "import secrets; print(secrets.token_urlsafe(32))"`） |
| `JWT_EXPIRE_HOURS` | Token 有效期（小时），默认 720（30 天） |
| `AUTH_REQUIRED` | 生产强制 `true` |
| `PLATFORM_API_KEYS` | 平台 API Key，格式 `"platform_id=key"`，多个用逗号或换行分隔。配置后接入方后端通过 `POST /auth/token` + `X-API-Key` 为自己用户换智能体 token |
| `ANONYMOUS_LOGIN_ENABLED` | 匿名登录开关；生产环境未显式设置时自动关闭 |

### 1.4 LLM 大模型（必填，方式二选一）

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | OpenAI 兼容；示例 `gpt-4o-mini` |
| `HY_API_KEY` / `HY_BASE_URL` / `HY_LLM_MODEL` / `HY_IMAGE_MODEL` | hy 大模型（推荐）；`HY_BASE_URL=https://tokenhub.tencentmaas.com/v1/responses`，`HY_LLM_MODEL=hy3` |
| `LLM_MAX_ITERATIONS` | 单轮工具调用最大次数（默认 8） |
| `LLM_REQUEST_TIMEOUT` | 单次 LLM 调用超时（秒，默认 120） |

> LLM 必须支持 Chat Completions 和 function / tool calling。

### 1.5 文生图（可选）

| 变量 | 说明 |
|------|------|
| `IMAGE_PROVIDER` | `mock` / `hy`（仅这俩已实现，其他值仍走 mock） |
| `IMAGE_API_KEY` / `IMAGE_BASE_URL` / `IMAGE_MODEL` | 按所选 provider 填写 |
| `IMAGE_WIDTH` / `IMAGE_HEIGHT` | 生成图默认尺寸（默认 768×1024） |
| `IMAGE_PUBLIC_BASE_URL` | 生图结果公网前缀（CDN / 对象存储域名）；空则用本服务 `/generated` |

### 1.6 微信小程序（可选）

| 变量 | 说明 |
|------|------|
| `WECHAT_APPID` | 微信 AppID |
| `WECHAT_SECRET` | 微信 AppSecret |

> 兼容旧名 `WX_APPID` / `WX_SECRET`，两组只需配置一组。仅启用 `/auth/wx-login` 时需要。

### 1.7 平台外部数据源（可选）

| 变量 | 说明 |
|------|------|
| `PLATFORM_DB_<SOURCE_ID>_URL` | 业务平台只读 PostgreSQL 连接串，如 `PLATFORM_DB_MAIN_URL`。服务端配置，LLM 不会看到；首期连接器支持 PostgreSQL |
| `PLATFORM_ORDER_API_URL` | 业务平台提供的**下单 API 地址**。智能体走此 URL 转发订单请求，**未配置时 `create_order` 工具直接报错、不写本地库** |
| `PLATFORM_ORDER_API_KEY` | 下单 API 所需凭据（按平台方约定） |

> 这三个变量**不在 `.env.example` 中固定声明**，按需添加。

### 1.8 CORS / 其他

| 变量 | 说明 |
|------|------|
| `ALLOWED_ORIGINS` | CORS 允许域名；开发可 `*`，生产建议明确域名列表 |
| `TENCENT_MAP_KEY` | 腾讯地图密钥（可选，目前未在主链路使用） |
| `REDIS_URL` | Redis 连接串（可选；目前限流 / 缓存由 Python 内存实现，配 Redis 暂不生效） |

`.env` 完整模板（示例）：

```bash
# ══════════════ 服务配置 ══════════════
APP_ENV=prod
HOST=0.0.0.0
PORT=8000

# ══════════════ 数据库 ══════════════
DATABASE_URL=postgresql://flora:REPLACE_WITH_POSTGRES_PASSWORD@postgres:5432/flora_agent
POSTGRES_PASSWORD=REPLACE_WITH_RANDOM_PASSWORD

# ══════════════ JWT / 多平台 / 鉴权 ══════════════
JWT_SECRET=change-this-to-a-random-secret-at-least-32-chars
JWT_EXPIRE_HOURS=720
AUTH_REQUIRED=true
PLATFORM_API_KEYS=wxmini=REPLACE_WITH_KEY_1,h5app=REPLACE_WITH_KEY_2
# ANONYMOUS_LOGIN_ENABLED=false

# ══════════════ LLM（任选一组）══════════════
# 方式 A：OpenAI 兼容
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_MAX_ITERATIONS=8
LLM_REQUEST_TIMEOUT=120

# 方式 B：hy（推荐）
# HY_API_KEY=sk-xxx
# HY_BASE_URL=https://tokenhub.tencentmaas.com/v1/responses
# HY_LLM_MODEL=hy3
# HY_IMAGE_MODEL=Hy-Image-3.0

# ══════════════ 文生图（可选）══════════════
IMAGE_PROVIDER=mock
IMAGE_API_KEY=
IMAGE_BASE_URL=
IMAGE_MODEL=
IMAGE_WIDTH=768
IMAGE_HEIGHT=1024
IMAGE_PUBLIC_BASE_URL=

# ══════════════ 微信小程序（可选）══════════════
WECHAT_APPID=
WECHAT_SECRET=

# ══════════════ 平台外部数据源（可选）══════════════
# PLATFORM_DB_MAIN_URL=postgresql://readonly_user:password@platform-db:5432/platform
# PLATFORM_ORDER_API_URL=https://platform.example.com/api/orders
# PLATFORM_ORDER_API_KEY=replace-with-platform-key

# ══════════════ 其他 ══════════════
ALLOWED_ORIGINS=https://your-miniprogram.com,https://your-h5.com
```

---

## 2. 数据库接入模型

智能体的存储分三层，**目标平台数据库与智能体内部数据库必须分离**：

| 层 | 存储 | 用途 |
|----|------|------|
| 内部运行面 | `DATABASE_URL` 指向智能体自己的 PostgreSQL | 会话、消息、记忆、任务、DIY 方案、映射草案与审计、配置项 |
| 外部业务面 | `PLATFORM_DB_<SOURCE_ID>_URL` 指向业务平台 PG（只读账号） | 商品 / 店铺 / 订单 / 用户实时查询 |
| 外部订单出口 | `PLATFORM_ORDER_API_URL` 调业务平台自有下单 API | 创建订单后转发，**不写本地库** |

SQLite 生产环境禁止使用。

### 2.1 外部数据库 7 步接入流程（与 [接入说明 §2.4](接入说明-花艺智能体API.md) 一致）

智能体不写本地商品 / 订单表，所以**只配连接串是不够的**，必须完成下方 7 步流程，缺一不可：

```text
① platform_db_test_connection(source_id)
       └─ 验证网络、账号、数据库类型
② platform_db_discover(source_id, sample_rows=0)
       └─ 拉表结构（默认 sample_rows=0 不读样本）
③ platform_db_sample_table(source_id, schema, table, limit)
       └─ 字段语义不明时才拉（最多 5 行脱敏）
④ platform_mapping_draft(profile, entity)
       └─ 生成映射草案（profile 含 plan / shop / order / user）
⑤ platform_mapping_save_draft(profile, draft)
       └─ 保存版本（status=draft → reviewed）
⑥ platform_mapping_set_status(profile, version, 'approved')
       └─ 平台方人工审核 → approved
⑦ platform_mapping_set_status(profile, version, 'active')
       └─ 激活；同一 source_id 同时只能有一个 active 版本
       └─ 旧 active 版本会被自动撤销（mapping_store.py 保证）
```

激活后 LLM 才能通过 `platform_db_query_entity(source_id, entity, keyword, shop_id?)` 查商品 / 店铺 / 订单。`shop_id` 参数是店铺锁定模式的关键——详见 [§6 店铺锁定模式](#6-店铺锁定模式)。

### 2.2 目标平台库连接器约束

- `platform_db_discover` 仅支持 PostgreSQL
- 所有外部连接走只读事务（`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`）
- `platform_db_sample_table` 默认不读取，最大 5 行
- `platform_db_query_entity` 只允许查询 active 映射
- 没有 active 映射时直接拒绝查询，LLM 会主动告知「平台未接入」

### 2.3 必映射的 4 个业务实体

| 实体 | 必须字段 | 锁定模式必须 |
|------|---------|--------------|
| `plan`（花束 / 商品） | `plan_id`、名称、价格、描述、图片、标签 | 映射 `shop_id`（所属店铺） |
| `shop`（店铺） | `shop_id`、名称、地址、评分、配送信息 | - |
| `order`（订单） | `order_id`、用户、店铺、方案、金额、状态、创建时间 | 映射 `shop_id` |
| `user`（用户） | `user_id`、昵称、头像 | - |

> `plan.shop_id` 与 `order.shop_id` 不映射时，店铺锁定退化为「LLM 按返回行内字段自行筛」（不可靠）。生产接入请确保这两个字段映射正确。

### 2.4 派生的下单契约

订单不在智能体内部表写，转发给业务平台：

```text
POST {PLATFORM_ORDER_API_URL}
Headers:
  Authorization: Bearer {PLATFORM_ORDER_API_KEY}
  Content-Type: application/json
Body:
  request_id          # 幂等键（同一 request_id 重复请求平台应去重）
  channel=flora_agent
  external_user_id    # 平台方体系内的用户标识（不要传派生 user_id）
  agent_session_id    # 智能体会话 ID（用于回写）
  shop_id             # 店铺
  plan                # 所选方案详情（含 plan_id / quantity / price / image）
  items[]             # 明细
  estimated_total     # 智能体核算金额
```

响应兼容以下字段（任一即可）：

```text
order_id ⇄ order_no ⇄ id ⇄ trade_id
total_price ⇄ total ⇄ amount ⇄ pay_amount
pay_url 或 pay 对象（含 payUrl / qrcode / prepay_id 等）
```

> `PLATFORM_ORDER_API_URL` 未配置时，智能体 `create_order` 工具直接抛错（`PLATFORM_ORDER_API_URL not configured`），不会尝试写本地库，也不会编造订单号。

---

## 3. 数据库与生图迁移

如果部署方使用已有业务数据库，不要求覆盖平台现有表。建议按以下顺序操作：

1. DBA 检查目标 PostgreSQL 数据库和应用账号。
2. 若应用账号有建表权限，直接启动服务，`init_db()` 会创建缺失运行时表（sessions/messages/memories/image_tasks/mapping_drafts/mapping_audit/diy_plans/notifications/operations_config 等）。
3. 若应用账号没有 DDL 权限，DBA 执行 [`migrations/001_image_tasks.sql`](migrations/001_image_tasks.sql)，并确认应用账号拥有 `image_tasks` 的 `SELECT` / `INSERT` / `UPDATE` 权限。
4. 启动服务，确认日志通过数据库初始化和 `image_tasks` 自检。
5. 用 `POST /chat` 触发生图 / 贺卡任务，再用 `GET /tasks/{task_id}` 验证状态能查询，图片能通过 `/generated/{filename}.png` 拿到。

---

## 4. 支持的图像生成提供商

| Provider | 说明 | 状态 |
|---------|------|------|
| `mock` | 仅用于开发联调，不产生真实图片 | **已实现** |
| `hy` | hy 大模型图像生成（`Hy-Image-3.0`） | **已实现** |

> 其他 provider（`flux` / `dall-e` / `kling` / `comfyui`）目前仅保留配置占位，未在当前代码中实现；写入 `.env` 会自动回落 `mock`。**AI 贺卡的模板合成不依赖外部 provider**，详见 [§5](#5-ai-电子贺卡与字体)。

---

## 5. AI 电子贺卡与字体

贺卡在容器内用 Pillow 模板合成（**不调外部 provider**），需要中文字体支持。

### 5.1 字体降级链

环境变量 `CARD_FONT_PATH` 未配置时，自动按以下顺序探测：

```text
1. CARD_FONT_PATH                  # 用户自定义（推荐挂自定义字体时使用）
2. /usr/share/fonts/truetype/wqy/wqy-microhei.ttc    # 容器内（Dockerfile 装好）
3. /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
4. C:\Windows\Fonts\msyh.ttc                          # Windows 主机
5. /System/Library/Fonts/PingFang.ttc                # macOS 主机
```

### 5.2 Dockerfile 字体选择（重要）

`Dockerfile` 当前装的是 **文泉驿微米黑 (`fonts-wqy-microhei`, 约 5MB)**，并切换到阿里云镜像源。**原因**：

- 旧的 `fonts-noto-cjk`（300MB+）在国内服务器 apt 卡死（apt 进程无网络流量），多次实测超时
- 文泉驿微米黑字符覆盖已足够贺卡模板（含简繁与日常符号），且体积小、构建快

如果需要更换字体（如 Noto Sans CJK）：

```dockerfile
# 编辑 Dockerfile，替换 apt 安装行后重新构建镜像
RUN apt-get update && apt-get install -y fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
```

或在 `.env` 直接覆盖路径：

```env
CARD_FONT_PATH=/path/to/your.ttf
```

### 5.3 模板与工具

- 5 套模板：`warm` / `blush`（默认）/ `green` / `letter` / `night`
- 规格：900×1200 竖版
- 工具：`suggest_greetings(recipient, occasion, style)`（47 条情景词库）+ `render_greeting_card(text, recipient, sender, template?)`
- 输出：`{image_url, text, recipient, sender, template, note}`，图片推送至 `/generated/greet_*.png`

### 5.4 故障排查

| 现象 | 可能原因 |
|------|---------|
| 贺卡图里中文变 □ / 乱码 | 容器内没装中文字体；裸机缺系统字体包 |
| 生成的图极大（PNG > 1MB） | 字体探测链找到大字体文件，OK，无须处理 |
| `render_greeting_card` 返回 `{error}` | 见日志（`agent/skills/skill_greeting.py`），通常是字体未找到或 `text` 为空 |

---

## 6. 店铺锁定模式

会话一旦带 `shop_id` 进入，整轮会话**锁定在该店铺**。锁定状态由 `backend/storage/mem_store.get_session_shop_id(session_id)` 持久化，**整轮不变**。

### 6.1 触发方式

前端在 `/chat` 与 `/conversations` 请求 body 中带 `shop_id`：

```json
POST /chat
{
  "message": "我想送妈妈一束康乃馨",
  "user_id": "wxmini_bc945f036d8ab07924aff191",
  "session_id": "conv_xxx",
  "shop_id": "SHOP_001"
}
```

`shop_id` 可选，缺省即为未锁定模式（普通入口）。

### 6.2 4 层联动约束

| 层 | 文件 / 工具 | 约束 |
|----|------------|------|
| ① 数据层 | `backend/data_gateway/external.py::query_external_entity` | `shop_id` 参数映射含 `shop_id` 列时 `CAST("col" AS text) = %s` WHERE 过滤（兼容整型店铺 ID），与 keyword 以 `AND` 组合 |
| ② 工具层 | `agent/data_tools.py::platform_db_query_entity` | `inject_context=True`；漏传自动套用会话锁定；**传别店 shop_id 一律拒绝**；返回 `meta.shop_scoped / filtered_by`（`mapping_shop_column` 或 `none_needs_model_filter`，后者提示模型按行内 `shop_id` 自行筛） |
| ③ DIY 层 | `agent/diy_tools.py::{generate,revise,design}_diy_plan` | 提示词注入「只能选该店在售花材，不确定先查 `platform_db_query_entity`」；方案写 `shop_id`；改版继承原方案店铺范围 |
| ④ 下单 | `agent/skills/skill_order.py::create_order` | 锁定自动填充（含 `first` 占位）；传别店被拒；方案 `plan['shop_id']` 与锁定不一致被拒；回读平台方案带 `shop_id` 过滤 |

### 6.3 行为差异表

| 行为 | 未锁定 | 锁定 |
|------|-------|------|
| 询问「去哪家店」 | 是 | 否 |
| 推 `shop_card` | 是 | 否 |
| DIY 选材范围 | 知识库全部 | 仅该店在售（平台库验证） |
| 下单 `shop_id` | 用户确认 / 选第一项 | 自动用锁定店铺 |
| 跨店方案 | 允许 | 拒绝 |
| 缺货时 | 推其他店 | 给本店替代方案，不拿别家凑 |

### 6.4 退出锁定

暂无显式接口。如需让用户切到其他店铺：**重建会话**——前端调 `POST /conversations` 新建一个 `session_id`，再带新 `shop_id` 调 `/chat`。

### 6.5 接入方配合项

- **前端**：从店铺页入口调用 `/chat` 时把 `shop_id` 放进 body
- **平台方**：必须映射 `plan.shop_id` 和 `order.shop_id`（见 [§2.3](#23-必映射的-4-个业务实体)）
- **运维**：脱敏日志里出现 `shop_id` 是预期行为，不要当成泄漏；`platform_db_query_entity` 返回的 `meta.shop_scoped` 是排查锁定的关键证据

---

## 7. API 一致性说明

当前代码中的 `/chat` 返回字段为：`reply`、`ui`、`data`、`action`、`tool_calls`、`session_id`、`stage`。

`/chat/stream` 使用 SSE，**真实事件（线上实测枚举，以 `agent/agent.py::arun_stream` 为准）**：

| 事件 | 说明 |
|------|------|
| `tool_call` | 工具调用状态（含工具名、阶段、入参摘要） |
| `text` | 文本回复的**增量片段**，前端需自己拼接 |
| `card` | UI 卡片数据（结构同 `/chat` 响应里的 `ui + data`） |
| `done` | 对话结束（正常结束一定带此事件） |
| `error` | 错误信息 |

> 注：历史文档曾经提到 `thinking` 与 `tool_result` 事件，但实测**没有这两类事件**。统一以来源为准。

`GET /ui-contract` 返回的 UI 列表（8 种）：`text` / `dialog_options` / `plan_card` / `shop_card` / `order_card` / `pay_jump` / `image_task` / `greeting_card`。

---

## 8. 部署检查

启动后至少验证：

| 顺序 | 接口 / 命令 | 期望 |
|------|------------|------|
| 1 | `GET /health` | `{"status":"ok","service":"flora-agent","version":"1.0.0","env":"prod"}` |
| 2 | `GET /ui-contract` | 200，返回 UI 清单 |
| 3 | `POST /auth/token`（带 `X-API-Key` + `external_user_id`） | 换 JWT，校验派生 `user_id` |
| 4 | `POST /auth/me` | 用换到的 token 读回用户信息 |
| 5 | `POST /chat` | 200，`stage=plan_recommend` 或 `greeting_card` 等 |
| 6 | `POST /chat/stream` | SSE 长连接不中断，能拼出 `text` 与 `done` |
| 7 | `GET /tasks/{task_id}` | 生图 / 贺卡任务状态查询 |
| 8 | `GET /generated/{filename}.png` | 静态图可下载 |

可选：

- `POST /auth/wx-login`（如果启用 `/auth/wx-login` 路径）
- 锁定场景：带 `shop_id` 调 `/chat`，校验 `meta.shop_scoped` / 自动填 `shop_id`
- 下单场景：检查 `PLATFORM_ORDER_API_URL` 是否被正确使用，错误降级是否如预期

---

## 9. API 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/auth/token` | 通用平台接入：`X-API-Key` + `external_user_id` → JWT |
| `POST` | `/auth/wx-login` | 微信登录（jscode2session → JWT） |
| `POST` | `/auth/anonymous` | 匿名登录（仅开发联调，生产默认关闭） |
| `GET` | `/auth/me` | 当前用户信息 |
| `POST` | `/chat` | 对话（同步，返回 `reply / ui / data / action / stage`） |
| `POST` | `/chat/stream` | 对话（SSE 流式：`tool_call / text / card / done / error`） |
| `POST` | `/chat/reset` | 删除会话 |
| `GET` | `/conversations` | 会话列表（含每个会话的 `shop_id` 锁定态） |
| `POST` | `/conversations` | 创建会话（可带 `shop_id` 触发锁定） |
| `GET` | `/conversations/{id}/messages` | 会话消息 |
| `GET` | `/tasks/{task_id}` | 生图 / 贺卡任务状态 |
| `GET` | `/generated/{filename}.png` | 生图结果（静态托管） |
| `GET` | `/ui-contract` | UI 契约（接入方对照组件清单） |
| `GET` | `/health` | 健康检查 |

---

## 10. 多平台接入流程

各平台统一走「平台 API Key + 用户标识」：

1. 部署方在 `PLATFORM_API_KEYS` 为每个接入平台配置 `platform_id=key`。
2. 接入方后端认证自己的终端用户（各平台用自己的登录体系：小程序 `wx.login` / H5 自行登录 / App 设备 ID 等）。
3. 接入方后端携带 `X-API-Key` 请求 `POST /auth/token`，body 为：
   ```json
   { "external_user_id": "该平台体系内的用户标识" }
   ```
4. 拿返回的 `access_token` 与 `user_id`（派生值，格式 `wxmini_<hex>`）调 `/chat` 等业务接口。

**关键校验**：`/chat` / `/conversations` 请求中的 `user_id` 必须等于 token 里的派生 `user_id`，否则 403。

微信小程序可选两条路：

- **走后端统一换 token**（推荐多平台一致）：接入方后端拿 `wx.login` 的 code 自行换 openid / unionid，再走 `POST /auth/token`
- **使用内置 `/auth/wx-login`**：智能体直接调微信 `jscode2session`（需配 `WECHAT_APPID` / `WECHAT_SECRET`）

---

## 11. 常见问题

### Q: LLM 调用报错 401？

A: 检查 `LLM_API_KEY` 是否正确、是否过期。Docker 内若连外网失败，先确认 `Dockerfile` 镜像源是否切阿里云（apt 卡死通常是没切源）。

### Q: 文生图不生成？

A: 当前代码仅 `mock` + `hy` 两个 provider 实现；其他 provider 写入 `.env` 仍走 mock。检查 `IMAGE_PROVIDER` / `IMAGE_API_KEY` / `IMAGE_BASE_URL` / `IMAGE_MODEL`。

### Q: AI 贺卡中文变 □ / 乱码？

A: 容器内缺中文字体。Docker 部署应该没事（已装 `fonts-wqy-microhei`）；裸机部署运行 `apt-get install fonts-wqy-microhei`（Debian/Ubuntu），或在 `.env` 配 `CARD_FONT_PATH` 指向本机字体路径。

### Q: 微信登录失败？

A: 检查 `WECHAT_APPID` 和 `WECHAT_SECRET` 是否与小程序后台一致；旧名 `WX_APPID` 和 `WX_SECRET` 仍兼容。

### Q: 登录接口返回 401 / 503？

A: 确认已配置至少 32 位 `JWT_SECRET`。微信登录还需 `WECHAT_APPID` / `WECHAT_SECRET`；匿名登录也需要 `JWT_SECRET`。

### Q: `POST /chat` 返回 403？

A: 请求体 `user_id` 与 `/auth/token` 返回的派生 `user_id` 不一致。生产环境 token 与 user_id **必须成对出现**，从接入方后端一起下发。

### Q: 数据库连接失败？

A: 检查 `DATABASE_URL` 是否为可访问的 PostgreSQL 地址、用户名 / 密码 / 端口是否正确，以及部署环境是否允许访问数据库。驱动由 `psycopg[binary]` 提供；**生产环境不支持 SQLite**。

### Q: 平台只读库未配置 / 查不到商品？

A:
1. 必须填 `PLATFORM_DB_<SOURCE_ID>_URL`（如 `PLATFORM_DB_MAIN_URL`）
2. 必须完成 [§2.1](#21-外部数据库-7-步接入流程与-接入说明-§24-一致) 的 7 步流程、激活 mapping
3. 没 active mapping 时 LLM 会如实告知「未接入」——这是**设计如此**，不是 bug

### Q: 下单接口返回错误？

A:
1. 必须配 `PLATFORM_ORDER_API_URL`（可选 `PLATFORM_ORDER_API_KEY`），否则 `create_order` 工具直接抛错
2. 锁定模式下「跨店拒绝」、方案归属校验失败也会拒绝
3. 错误信息经 `order_card.data.error` 回灌前端

### Q: 店铺锁定模式下为何不能切店？

A: 设计如此。如需切店，**重建会话**（前端调 `POST /conversations` 新建，前端再带新 `shop_id` 调 `/chat`）。

### Q: 小程序请求被 CORS 拦截？

A: 检查 `ALLOWED_ORIGINS` 是否包含小程序域名（不含路径），多个用逗号分隔。

### Q: 工具调用超时？

A: 调大 `LLM_REQUEST_TIMEOUT`（默认 120 秒）；Nginx 反代同步调大 `proxy_read_timeout`（默认 300 秒）；SSE 必须 `proxy_buffering off`。

### Q: 如何添加新工具？

A:
1. 在 `agent/` 下新建模块（如 `agent/my_tools.py`）
2. 用 `@register_tool(name=..., inject_context=..., description=...)` 装饰
3. 在 `agent/__init__.py` 加 `from . import my_tools`（**否则工具不会被注册**）
4. 重建 agent 镜像：`docker compose up -d --build agent`

### Q: 怎样对接 image_task / greeting_card？

A: 拿到 `data.task_id` 后轮询 `GET /tasks/{task_id}`，直到 `status=done`。`result_url` 即图片相对地址；生产建议配 `IMAGE_PUBLIC_BASE_URL` 把 `result_url` 改为 CDN 地址。

---

## 12. 知识库扩充规则

当前知识库采用「核心本地库 + 扩展来源」方案。

### 12.1 核心本地库

默认直接读取：

- `agent/knowledge/flowers.json`
- `agent/knowledge/styles.json`
- `agent/knowledge/scenes.json`
- `agent/knowledge/pairings.json`
- `agent/knowledge/budget.json`
- `agent/knowledge/packaging.json`

### 12.2 扩展来源

后续扩充用，不建议直接混进核心库：

- `PlantFlowerDatasets`
- `flower-db`
- `Flower-Knowledge-Graph-Visualization`
- `flora-atlas`

### 12.3 入库原则

1. 先标准化字段，再写入本地。
2. 核心库保留高频、稳定、业务强相关内容。
3. 扩展源优先写入 `agent/knowledge/sources/` 下的标准化文件。
4. 经过验证的高质量内容，再从 sources 合并回核心 JSON。
5. 所有新增数据都应带 `source` 字段，便于追溯。

### 12.4 推荐字段

```json
{
  "id": "F_ROSE_EXT",
  "name": "玫瑰",
  "aliases": ["红玫瑰", "粉玫瑰"],
  "tags": ["主花", "浪漫"],
  "source": "PlantFlowerDatasets",
  "flower_language": ["爱情", "浪漫"],
  "care": { "light": "充足散射光", "water": "见干见湿" },
  "season": ["四季"],
  "colors": ["红", "粉"]
}
```

### 12.5 导入脚本

入口：`scripts/import_flower_knowledge.py`

- 读取知识库清单
- 统一外部记录格式
- 为后续批量灌入预留入口

---

## 13. 封装边界说明

这个包已经封装好的部分：

- 智能体主逻辑 `agent/`（含 ReAct 主循环、工具注册、平台只读适配、订单编排、AI 贺卡）
- 本地知识库 `agent/knowledge/`
- 后端存储 `backend/storage/`（会话、消息、记忆、任务、映射、DIY 方案）
- 数据网关 `backend/data_gateway/`（平台只读连接器 + 实体映射存储）
- HTTP 接口 `backend/routers/`（auth/chat/conversations/tasks/ui-contract）
- 部署入口 `main.py` + Dockerfile + docker-compose.yml
- 文档 `README.md` / `DEPLOY.md` / `FRONTEND_CONTRACT.md` / `接入说明-花艺智能体API.md`

需要部署方自行配置的部分：

- LLM API Key / Base URL / Model
- 文生图 API Key（按所选 provider）
- 微信小程序 AppID / Secret（如果启用 `/auth/wx-login`）
- 数据库实际连接信息（`DATABASE_URL` / `POSTGRES_PASSWORD`）
- 生产域名、CORS、回调地址
- 至少 32 位 `JWT_SECRET`
- 多平台 API Key（`PLATFORM_API_KEYS`）
- 平台只读库连接串（`PLATFORM_DB_<SOURCE_ID>_URL`）
- 平台下单 API（`PLATFORM_ORDER_API_URL`）

原则：

1. 我们负责封装代码和默认能力。
2. 部署方负责填充生产凭据和基础设施地址。
3. 平台未实现"方案页 / 订单页 / 支付跳转"等能力时，可先按智能体返回的 `action` 做文本降级，不要阻断完整业务流程。
4. 平台接入时应优先检查 `required_capabilities`，按能力表逐步补齐前端页面与业务接口。

---

## 14. 排障工具箱

### 14.1 服务启动失败

```bash
docker compose logs agent | head -100
# 常见：
#   - DATABASE_URL 连不上 → 检查 postgres 容器 healthy
#   - JWT_SECRET 未配置 / 长度 < 32 → 直接启动失败（生产强制）
#   - 工具注册为空（TOOL_REGISTRY 为空）→ 检查 agent/__init__.py 是否把所有模块 import 了
```

### 14.2 工具不响应（LLM 拿不到工具）

```bash
# 在容器内查工具注册表
docker exec flora-agent python -c "
import sys; sys.path.insert(0, '/app')
from agent.toolkit import TOOL_REGISTRY
print('tools:', len(TOOL_REGISTRY))
print(sorted(TOOL_REGISTRY.keys()))
"
```

期望 18 个工具。若输出为空或缺失某些工具，说明 `agent/__init__.py` 没有 import 对应模块 —— 修复后 `docker compose up -d --build agent` 重建。

### 14.3 SSE 中断 / 收不到 `done`

- Nginx：`proxy_buffering off;` + `proxy_read_timeout 300s;`
- 反代：避免压缩 / 启用 HTTP/1.1
- 客户端：用心跳超时，不要用 `fetch().then()` 等待首字节
- 服务端：`docker logs flora-agent | tail -50` 看 `agent/agent.py::arun_stream` 报错

### 14.4 平台只读库连不上

```bash
# 从智能体容器内手动测连通性
docker exec flora-agent psql "$PLATFORM_DB_MAIN_URL" -c '\dt'
# 期望列出业务表；如果是 SSL / 网络问题，会在容器日志里看到具体报错
```

### 14.5 定位店铺锁定

```bash
docker exec flora-agent python -c "
import sys; sys.path.insert(0, '/app')
from backend.storage import mem_store
# 把目标 session_id 替换进去
print(mem_store.get_session_shop_id('conv_xxx'))
"
```

期望返回当前会话锁定的 `shop_id` 或 `None`。如果返回 `None`，说明这个会话从未带过 `shop_id`，谈不上锁定。

### 14.6 贺卡图渲染失败

```bash
# 容器内直接试
docker exec flora-agent python -c "
import sys; sys.path.insert(0, '/app')
from agent.skills.skill_greeting import render_greeting_card
print(render_greeting_card('测试', '妈妈', '小明', 'blush'))
"
```

返回结构 `{image_url: ...}` 即成功；返回 `{error}` 时按 message 排查（通常是字体路径或文案为空）。

### 14.7 日志关键字速查

| 关键字 | 含义 |
|--------|------|
| `agent_session_start user=` | 新会话建立（含派生 `user_id`） |
| `tool_call name=... ok=... ms=...` | 工具调用概要 |
| `tool_result meta.shop_scoped=...` | 店铺锁定生效证据 |
| `create_order error: PLATFORM_ORDER_API_URL not configured` | 下单 API 未配，按设计报错 |
| `mapping set_status active (撤下旧 active)` | 映射切换生效 |
| `FATAL init_db: image_tasks table missing` | DDL 权限不足，按 [§3](#3-数据库与生图迁移) 处理 |

---

完整配置参考 [.env.example](.env.example)、[README.md](README.md)、[FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)、[接入说明-花艺智能体API.md](接入说明-花艺智能体API.md)。
