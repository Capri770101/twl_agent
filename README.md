# 🌸 Flora Agent - 跳舞兰花卉智能体

> 花语释义：跳舞兰（文心兰）花语为「快乐无忧·活泼灵动」，代表发现生活之美的雀跃心情。本智能体以跳舞兰为品牌化身，陪用户把心意变成花。

基于 ReAct 架构的花艺顾问 AI 系统，已重构成**纯后端 API**——部署到服务器后由微信小程序、H5、App 等多平台通过 HTTP 调用。内置专业的花艺设计建议、效果图像生成、电子贺卡同步出图、店铺锁定模式，以及面向多业务平台的**只读数据适配**。

> 📘 接入方必读三件套（按顺序）：
> 1. [接入说明-花艺智能体API.md](接入说明-花艺智能体API.md) —— 「角色 × 待办清单」，告诉你接入时要做什么、谁来做
> 2. [FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md) —— UI 数据契约，前端按这套做组件
> 3. [DEPLOY.md](DEPLOY.md) —— 部署方视角的环境变量、Docker、运维排障

---

## 📋 目录

- [项目特性](#项目特性)
- [系统架构](#系统架构)
- [快速开始](#快速开始)
- [环境配置](#环境配置)
- [API 接口文档](#api-接口文档)
- [部署前必须配置的内容](#部署前必须配置的内容)
- [完整业务流程](#完整业务流程)
- [店铺锁定模式](#店铺锁定模式)
- [平台接入契约](#平台接入契约)
- [前端对接契约](#前端对接契约)
- [AI 电子贺卡](#ai-电子贺卡)
- [Vibe Coding 快速理解](#vibe-coding-快速理解)
- [知识库管理](#知识库管理)
- [部署指南](#部署指南)
- [常见问题](#常见问题)

---

## 🌟 项目特性

- **ReAct 智能体架构**：基于推理-行动循环的智能对话系统，工具集中收敛为业务必需
- **多平台中立接入**：统一通过 `POST /auth/token`（X-API-Key + `external_user_id`）换 JWT，业务平台负责认证自己的终端用户
- **微信小程序原生支持**：内置 `/auth/wx-login` 直连微信 `jscode2session`；也可走后端统一换 token
- **平台只读数据接入**：商品/店铺/订单**全部通过 `PLATFORM_DB_<SOURCE_ID>_URL` 实时查平台库**，本地不存商品/订单镜像
- **店铺锁定模式**：从某店铺页进入时全程限定该店，会话绑定 `shop_id`，4 层联动（数据/工具/DIY/下单）
- **DIY 方案与效果图**：根据需求生成花束方案（花材、数量、步骤、预算、养护），并异步生成配套预览图
- **AI 电子贺卡**：47 条情景词库 + 5 套模板（warm/blush/green/letter/night），下单后可一键生成竖版贺卡
- **SSE 流式响应**：实时返回智能体的 `tool_call / text / card / done / error` 事件流
- **三级鉴权**：`/auth/wx-login`（小程序）/ `/auth/token`（多平台通用）/ `/auth/anonymous`（开发联调；生产默认关闭）

---

## 🏗️ 系统架构

```
flora_agent_package/
├── main.py                       # FastAPI 服务入口（title="跳舞兰花卉智能体 API"）
├── agent/                        # 智能体核心模块
│   ├── agent.py                  # ReAct 智能体主循环（场景1-7 + 店铺锁定分支）
│   ├── ports.py                  # 跨模块 Protocol 契约（FlowerRequirement / PlanRepository 等）
│   ├── requirements.py           # 需求抽取（人数/预算/花材数/场景/色系等）
│   ├── toolkit.py                # 工具注册表 + execute_tool 上下文注入 + MCP 白名单
│   ├── tools.py                  # 花艺核心工具（知识检索 + 方案终结工具 show_plan_card 等）
│   ├── data_tools.py             # 平台只读工具集（platform_db_* / platform_mapping_*，含店铺锁定 SQL 过滤）
│   ├── diy_tools.py              # DIY 方案（generate_diy_plan / revise_diy_plan / generate_effect_image）
│   ├── memory_tools.py           # 用户级/会话级记忆与偏好读写
│   ├── skills/
│   │   ├── skill_order.py        # 下单编排（自动填充锁定店铺 shop_id、越店拒绝）
│   │   └── skill_greeting.py     # AI 贺卡（suggest_greetings 47 条词库 + render_greeting_card 5 模板）
│   ├── engine/                   # 运行引擎
│   │   ├── ui_protocol.py        # UIType 枚举（8 种）+ ChatResponse + AgentAction + GreetingCard 模型
│   │   ├── llm.py                # LLM provider 适配与多 key 路由
│   │   ├── budget.py             # 单轮/单用户预算计数与限速
│   │   ├── circuit_breaker.py    # provider 故障熔断
│   │   └── state.py              # 会话状态机 + 店铺锁定状态读写
│   └── knowledge/                # 本地花艺知识库（JSON + store.py）
├── backend/                      # 后端服务
│   ├── config.py                 # pydantic-settings 配置
│   ├── auth.py                   # JWT / 多平台 Key / 派生 user_id
│   ├── routers/                  # auth / chat / conversations / tasks / ui-contract
│   ├── storage/                  # 内部 PostgreSQL（sessions / messages / memories / image_tasks / mapping_drafts / diy_plans / notifications / operations_config / mapping_audit 等运行时表）
│   └── data_gateway/             # 平台只读连接器 + 实体映射
│       ├── external.py           # 只读事务 + CAST("col" AS text) 店铺过滤
│       ├── mapper.py             # tool_result(ok, data, error, meta=None) + 实体映射存储
│       ├── mapping_store.py      # 映射版本（draft → reviewed → approved → active）
│       └── gateway.py            # 外部只读查询编排
├── scripts/                      # 维护与运维脚本（verify / install_cert / import_knowledge / ...）
├── deploy/                       # 部署资源（nginx.conf / renew-cert.sh / 广州ECS部署清单.md / certs/）
├── .env.example                  # 环境变量模板（不含真实密钥）
├── migrations/                   # DDL（image_tasks 表等）
├── Dockerfile                    # 镜像（python 3.12-slim + fonts-wqy-microhei + 阿里云镜像源）
├── docker-compose.yml            # 三服务编排（postgres / agent / 可选 nginx）
├── README.md                     # ← 你在这里
├── DEPLOY.md                     # 部署细节与排障
├── FRONTEND_CONTRACT.md          # 前端 UI 数据契约
└── 接入说明-花艺智能体API.md      # 接入方必读（角色 × 待办清单）
```

> **已移除（不再存在）**：本地商品/店铺/订单镜像表（plans / shops / shop_plans / categories / orders / order_items）已下线；`shop_tools.py` 与 `search_plans` / `search_shops` / `db_auto_*` / `data_mapping.json` / `write_enabled=true` 流程均已废弃，新接入请按 [接入说明 §1 废弃词表](接入说明-花艺智能体API.md) 对照。

---

## 🚀 快速开始

### 方式一：Docker 部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/Capri770101/twl_agent.git
cd flora_agent_package

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env：填 LLM_API_KEY / JWT_SECRET / PLATFORM_API_KEYS / POSTGRES_PASSWORD 等

# 3. 启动服务
docker compose up -d --build

# 4. 查看日志
docker compose logs -f agent

# 5. 验证服务
curl http://localhost:8000/health
# 期望：{"status":"ok","service":"flora-agent","version":"1.0.0","env":"prod"}
```

### 方式二：本地开发

```bash
# 1. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env：把 APP_ENV 改成 dev

# 4. 启动服务
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 5. 跑端到端冒烟
python scripts/verify.py
```

> 本地默认走 PostgreSQL（`DATABASE_URL` 优先 docker-compose 内 `postgres` 容器）；macOS/Windows 可直接装本机 PostgreSQL 后改 `.env` 指向本机。

---

## ⚙️ 环境配置

### 必填：模型服务（二选一）

**方式 A：OpenAI 兼容接口**

| 变量 | 说明 | 示例 |
|------|------|------|
| `LLM_API_KEY` | LLM API 密钥 | `sk-xxx` |
| `LLM_BASE_URL` | LLM API 地址 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 模型名称 | `gpt-4o-mini` |

**方式 B：hy 大模型（推荐）**

| 变量 | 说明 | 示例 |
|------|------|------|
| `HY_API_KEY` | hy 大模型 API 密钥 | `sk-xxx` |
| `HY_BASE_URL` | hy 大模型 API 地址 | `https://tokenhub.tencentmaas.com/v1/responses` |
| `HY_LLM_MODEL` | LLM 模型名称 | `hy3` |
| `HY_IMAGE_MODEL` | 图像生成模型名称 | `Hy-Image-3.0` |

### 必填：数据库与鉴权

| 变量 | 说明 | 默认 |
|------|------|------|
| `DATABASE_URL` | 内部 PostgreSQL 连接串 | `postgresql://flora:密码@postgres:5432/flora_agent` |
| `JWT_SECRET` | JWT 签名密钥（生产 ≥ 32 位） | - |
| `POSTGRES_PASSWORD` | docker-compose 内 postgres 容器密码 | - |

### 可选：图像生成

| 变量 | 说明 | 默认 |
|------|------|------|
| `IMAGE_PROVIDER` | 图像生成 provider | `mock` |
| `IMAGE_API_KEY` / `IMAGE_BASE_URL` / `IMAGE_MODEL` | 按所选 provider 填写 | - |
| `IMAGE_PUBLIC_BASE_URL` | 生图结果公网前缀（CDN/对象存储域名） | - |

### 可选：多平台接入与小程序

| 变量 | 说明 | 默认 |
|------|------|------|
| `PLATFORM_API_KEYS` | 平台 API Key，格式 `platform_id=key`，逗号/换行分隔 | - |
| `WECHAT_APPID` / `WECHAT_SECRET` | 微信小程序（兼容 `WX_APPID` / `WX_SECRET`） | - |
| `ALLOWED_ORIGINS` | CORS 允许域名（生产建议明确域名） | `*` |

### 可选：外部只读数据源（按数据源单独配置）

```env
PLATFORM_DB_MAIN_URL=postgresql://readonly_user:password@platform-db:5432/platform
PLATFORM_ORDER_API_URL=https://platform.example.com/api/orders
PLATFORM_ORDER_API_KEY=replace-with-platform-key
```

> `PLATFORM_DB_<SOURCE_ID>_URL` 走平台只读连接器；`PLATFORM_ORDER_API_URL` 配对宿主平台的下单 API（智能体负责组装请求、转发到该 URL，不写本地库）。两者不在 `.env.example` 中固定声明，按需加。

### 支持的 LLM 提供商

| 提供商 | Base URL | 模型示例 |
|--------|----------|----------|
| hy 大模型（推荐） | `https://tokenhub.tencentmaas.com/v1/responses` | `hy3` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b` |

### 支持的图像生成提供商

| Provider | Base URL | 模型示例 | 状态 |
|---------|----------|----------|------|
| `mock` | 本地内置 | - | **已实现**，开发联调 |
| `hy` | `https://tokenhub.tencentmaas.com/v1/responses` | `Hy-Image-3.0` | **已实现** |
| `flux` / `dall-e` / `kling` / `comfyui` | - | - | 预留声明，需补充实现 |

> 注：当前代码仅 `mock` + `hy` 两路可用；其他 provider 写入 `.env` 仍会落到 mock，不会真生图。

**生图结果存储**：PNG 写入本地 `data/generated/{task_id}.png`，经本服务 `GET /generated/{task_id}.png` 访问；`/chat` 的 `image_task` 数据里 `result_url` 即该地址。若生产把图片托管到 CDN/对象存储，配置 `IMAGE_PUBLIC_BASE_URL=https://你的域名`，`result_url` 自动带上公网前缀。

完整配置请参考 [.env.example](.env.example) 和 [DEPLOY.md](DEPLOY.md)。

---

## 📡 API 接口文档

### 基础信息

- **Base URL**: `https://你的域名`（生产）/ `http://localhost:8000`（本地）
- **Content-Type**: `application/json`
- **认证方式**: `Authorization: Bearer <access_token>`

### 健康检查

```
GET /health
```

**响应示例**：

```json
{
  "status": "ok",
  "service": "flora-agent",
  "version": "1.0.0",
  "env": "prod"
}
```

### 登录接口

```text
POST /auth/token         # 通用平台接入（X-API-Key + external_user_id → JWT）
POST /auth/wx-login      # 微信小程序（jscode2session → JWT；需 WECHAT_APPID/WECHAT_SECRET）
POST /auth/anonymous     # 匿名登录（仅开发联调，生产默认关闭）
GET  /auth/me            # 当前用户信息
```

#### 多平台接入（推荐路径）

智能体独立部署后，各平台统一按「平台 API Key + 用户标识」接入：

```text
接入方前端 ──登录──> 接入方后端 ──X-API-Key + external_user_id──> POST /auth/token
                <──────────── 智能体 access_token ────────────
接入方前端 ──Bearer token──> 智能体 /chat、/chat/stream、/conversations ...
```

1. 部署方在 `.env` 的 `PLATFORM_API_KEYS` 中为每个接入方配置 `platform_id=key`
2. 接入方后端先认证自己的终端用户（小程序/H5/App 各用各的登录体系）
3. 接入方后端携带 `X-API-Key` 调用 `POST /auth/token`，body 提交该用户在接入方体系内的 `external_user_id`，换取智能体 JWT
4. 之后前端/接入方后端用该 JWT 调 `/chat`、`/conversations` 等业务接口

**信任模型**：持有 API Key 的一方负责认证终端用户；智能体的 `user_id` 由 `platform_id + external_user_id` 哈希派生，天然隔离各平台用户、不落盘原始标识，Key 轮换不影响用户身份稳定性。

微信小程序可选两条路：
- **走后端统一换 token**（推荐多平台一致）：接入方后端拿 `wx.login` 的 code 自己换 openid/unionid，再走 `POST /auth/token`
- **使用内置 `/auth/wx-login`**：智能体直接调微信 `jscode2session`（需配 `WECHAT_APPID` / `WECHAT_SECRET`，兼容 `WX_APPID` / `WX_SECRET`）

**关键校验**：`/chat` 等业务请求的 `user_id` 必须等于 `/auth/token` 返回的派生 `user_id`（形如 `wxmini_<32位 hex>`），否则 403。

> `/auth/anonymous` 仅用于开发联调：生产（`APP_ENV=prod`）未显式设置 `ANONYMOUS_LOGIN_ENABLED=true` 时自动禁用，避免匿名接口被刷 token 消耗模型额度。

当前仓库没有 MCP 全量工具桥接；如果宿主自行接入 MCP，只能使用 `agent.toolkit.get_mcp_tool_specs()` 的显式白名单。文件系统、数据库发现和外部数据库工具不得无条件暴露。MCP 通道只输出文本、图片 URL 和平台链接，不直接渲染 `ui/data` 卡片。

### 前端契约查询

```
GET /ui-contract
```

返回全量 UI 类型清单（数据字段 + 渲染要求 + 示例 payload），供前端 / AI 在接入时**程序化对照自己实现了哪些组件**。完整数据契约见 [FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)。

### 对话接口

```text
POST /chat          # 同步，返回完整结构化响应
POST /chat/stream   # SSE 流式，事件：tool_call / text / card / done / error
```

请求体包含：`message`、`user_id`、`session_id?`、`location?`、`shop_id?`（锁定店铺模式下从会话开始就传）、`image_url?`（参考图，可选）。生产环境由宿主平台完成用户登录，后端校验 `user_id` 与 token 一致。

`/chat` 返回字段：`reply`、`ui`、`data`、`action`、`tool_calls`、`session_id`、`stage`；其中 `ui` 由 `UIType` 决定（8 种：`text` / `dialog_options` / `plan_card` / `shop_card` / `order_card` / `pay_jump` / `image_task` / `greeting_card`），`action` 由 `AgentAction` 决定。

`/chat/stream` SSE 事件（实测枚举，以 `agent/agent.py::arun_stream` 为准）：

| 事件 | 说明 |
|------|------|
| `tool_call` | 工具调用状态（含工具名、阶段、入参摘要） |
| `text` | 文本回复的**增量片段**，前端需自己拼接 |
| `card` | UI 卡片数据（结构同 `/chat` 响应里的 `ui + data`） |
| `done` | 对话结束（正常结束一定带此事件） |
| `error` | 错误信息 |

> 与历史文档不同：实测中没有 `thinking` 和 `tool_result` 这两个事件。

### 会话管理

```
GET  /conversations?user_id=user_xxx              # 会话列表
GET  /conversations/{id}/messages?limit=50        # 会话消息
POST /conversations                               # 创建会话
POST /conversations/{id}/reset                     # 重置会话
```

### 图像 / 贺卡 / 任务查询

| 接口 | 说明 |
|------|------|
| `GET /tasks/{task_id}` | 生图任务状态（`done` 时 `result_url` 可用） |
| `GET /generated/{task_id}.png` | 生成图静态托管（生图结果 / 贺卡图） |

---

## 🚀 部署前必须配置的内容

部署前请按以下顺序完成配置。`.env.example` 只提供模板，不要把真实密钥提交到代码仓库。

### A. 必填：模型服务 + 数据库 + JWT

至少配置一套 LLM（OpenAI 兼容或 hy 大模型），以及内部数据库与至少 32 位 `JWT_SECRET`：

```env
LLM_API_KEY=你的模型密钥
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini

DATABASE_URL=postgresql://用户名:密码@数据库地址:5432/数据库名
JWT_SECRET=至少32位的随机字符串
```

LLM 必须支持 Chat Completions 和 function/tool calling。生产 / 多实例必须使用 PostgreSQL（或托管 PG）；本服务**不支持 SQLite**。Dockerfile 启动后会通过 `CREATE TABLE IF NOT EXISTS` 自建运行时表；应用账号无 DDL 权限时由 DBA 先执行 [`migrations/001_image_tasks.sql`](migrations/001_image_tasks.sql)。

### B. 必填：业务数据源（按接入说明 §2 流程）

> **新流程（2026-09 起）**：智能体**不写本地商品/订单表**。所有商品/店铺/订单通过平台只读连接器实时查询业务库，下单通过平台提供的 API 转发。所以**只配连接串是不够的**，必须完成下方 7 步接入流程，缺一不可。

**B1. 平台方开只读账号最小权限**（首期仅支持 PostgreSQL）：

```sql
CREATE USER flora_ro PASSWORD '...';
GRANT CONNECT ON DATABASE platform TO flora_ro;
GRANT USAGE ON SCHEMA public TO flora_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO flora_ro;
-- 未来视图/物化视图授权：GRANT SELECT ON future_tables TO flora_ro;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM flora_ro;
```

**B2. 配置连接串**：

```env
PLATFORM_DB_MAIN_URL=postgresql://flora_ro:...@platform-db:5432/platform
PLATFORM_ORDER_API_URL=https://platform.example.com/api/orders
PLATFORM_ORDER_API_KEY=replace-with-platform-provided-key
```

> `PLATFORM_DB_<SOURCE_ID>_URL` 服务端配置，LLM 不会看到连接串；`PLATFORM_ORDER_API_URL` 配平台方提供的下单 API，**未配置时 `create_order` 工具直接报错、不写本地库**。

**B3. 智能体侧 7 步接入流程**（运营/数据治理角色执行）：

```text
1) platform_db_test_connection(source_id)           # 验证网络+账号+数据库类型
2) platform_db_discover(source_id, sample_rows=0)   # 拉表结构（默认 sample_rows=0 不拉样本）
3) platform_db_sample_table(source_id, table, 5)    # 字段语义不明时才拉最多 5 行脱敏样本
4) platform_mapping_draft(profile)                  # 生成映射草案（plan/shop/order/user）
5) platform_mapping_save_draft(profile, draft)      # 保存为版本（draft → reviewed）
6) platform_mapping_set_status(..., approved)       # 平台方人工审核 → approved
7) platform_mapping_set_status(..., active)         # 激活；同时只能有一个 active 版本
```

激活后 LLM 才能通过 `platform_db_query_entity(source_id, entity, keyword)` 查商品/店铺/订单；`create_order` 会调 `PLATFORM_ORDER_API_URL` 转发。

**B4. 必映射的 4 个业务实体（最低字段要求）**：

| 实体 | 必须字段 | 可选映射 |
|------|---------|---------|
| `plan`（花束/商品） | `plan_id`、名称、价格、描述、图片、标签 | 所属店铺 `shop_id`、是否在售、季节 |
| `shop`（店铺） | `shop_id`、名称、地址、评分、配送信息 | 营业时间、城市、坐标 |
| `order`（订单） | `order_id`、用户、店铺、方案、金额、状态、创建时间 | 收货人、配送地址 |
| `user`（用户） | `user_id`、昵称、头像 | 手机号、收货地址 |

> `plan` 与 `order` 必须映射 `shop_id` 列，否则店铺锁定模式生效时只能由模型按返回行内字段自行筛选（不可靠）。

**B5. 已废词（不要再写进 schema / 工具名）**：本地 `data_mapping.json`、`write_enabled=true`、`db_auto_map`、`search_plans`、`search_shops`、`match_shop_items`、`db_discover`、`db_auto_*`、`db_sample_*` —— 这些是 2026-09 平台只读重构前的内部工具，新接入请走 `platform_db_*` / `platform_mapping_*`。

### C. 按需配置：图像生成与电子贺卡

#### C1. 图像生成（效果图）

```env
IMAGE_PROVIDER=mock
IMAGE_API_KEY=
IMAGE_BASE_URL=
IMAGE_MODEL=
```

- `mock`：适合开发联调，不产生真实图片（可在 `data/generated/` 留占位）
- `hy` / 其他 provider：必须填写 Key、Base URL、Model；当前代码仅 `mock` 与 `hy` 实现

#### C2. AI 电子贺卡

> 贺卡在容器内用 Pillow 模板合成（不调外部 provider），需要中文字体；`Dockerfile` 已通过 `apt-get install fonts-wqy-microhei` 装好文泉驿微米黑（约 5MB）并切换阿里云镜像源。如需自定字体，可挂：

```env
# 不写则按降级链自动找：/usr/share/fonts/truetype/wqy/wqy-microhei.ttc
#   → /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
#   → C:\Windows\Fonts\msyh.ttc（Windows 主机）
#   → /System/Library/Fonts/PingFang.ttc（macOS 主机）
CARD_FONT_PATH=/path/to/your.ttf
```

模板共 5 套：`warm`（奶油金边）/ `blush`（粉色浪漫，**默认**）/ `green`（自然清新）/ `letter`（信笺风）/ `night`（夜色氛围），竖版 900×1200。

### D. 按需配置：平台基础设施

```env
APP_ENV=prod
HOST=0.0.0.0
PORT=8000
ALLOWED_ORIGINS=https://你的平台域名
PLATFORM_API_KEYS=wxmini=sk-xxx,h5app=sk-xxx   # 多平台 API Key
WECHAT_APPID=                                   # 仅启用 /auth/wx-login 时需要
WECHAT_SECRET=                                  # 兼容 WX_APPID / WX_SECRET
```

部署方还需要准备：HTTPS 域名（含证书，Let's Encrypt 在国内服务器被墙，建议阿里云免费证书）、反向代理（Nginx，开 SSE）、数据库备份、日志采集、模型额度，以及目标平台的下单 API 与支付凭据。**支付密钥、用户身份认证和支付回调不能写入智能体代码**。

### E. 启动与自检

```bash
pip install -r requirements.txt
python -m compileall -q agent backend main.py
python -m uvicorn main:app --host 0.0.0.0 --port 8000
curl http://localhost:8000/health
```

启动后至少验证：`GET /health` / `POST /auth/token`（验证平台接入链路）/`POST /chat` / `POST /chat/stream` / `GET /tasks/{task_id}` / `GET /ui-contract`。生产推荐 Docker + Nginx，按 `DEPLOY.md` 走完整流程。

---

## 🔄 完整业务流程

智能体是「花艺需求 → 交易申请」业务智能体，不是只回答问题的聊天机器人。完整流程：

```text
用户输入需求（收花人 / 关系 / 场景 / 预算 / 风格 / 色系 / 位置）
  ↓
知识库/平台数据检索：花材、搭配、方案（plan）/ 店铺（shop）/ 库存/价格
  ↓
方案决策：在售商品 → plan_card；无现成 → DIY 定制 → diy_design → image_task
  ↓
用户修改或确认方案
  ↓
推荐可配送且具备花材/库存的店铺（未锁定模式下） / 锁定模式直接使用进入时的店铺
  ↓
用户确认店铺和方案
  ↓
创建订单（order_card）→ create_order 调 PLATFORM_ORDER_API_URL
  ↓
返回支付申请 / 跳转参数（pay_jump）
  ↓
可选：下单后引导生成电子贺卡（greeting_card）→ /generated/greet_*.png
  ↓
宿主平台打开支付页面并处理支付回调
```

智能体负责理解、检索、设计、编排；宿主平台负责渲染页面、接收用户按钮操作、执行真实订单/支付、处理登录和回调。智能体不会直接调用平台支付 SDK，也不应绕过用户确认创建真实支付。

### 关键业务行为

1. 用户只问花卉知识时优先回答知识问题，不强行推荐商品。
2. 用户提出购买需求时结合需求和真实平台数据推荐，不凭空编造库存/价格/店铺；**平台数据源未配置或映射缺失时必须如实告知**，绝不编造。
3. DIY 方案必须使用知识库中的真实花材、搭配和预算规则，返回可执行的花材数量、步骤、养护与 `effect_prompt`。
4. 方案卡片、文本回复、订单金额必须保持一致；贺卡模板与正文一致。
5. 预览图 / 贺卡图都是异步任务，平台不能把任务 ID 当图片 URL。
6. 下单前必须有明确确认；订单写入必须可追踪、可幂等，支付结果以平台回调为准。

---

## 🔒 店铺锁定模式

当用户从某具体店铺页（如小程序店铺详情、H5 店铺落地页）进入对话时，前端在 `/chat` 与 `/conversations` 中带 `shop_id`，整个会话**锁定在该店铺**，不再选店、不推其他店铺方案、不询问「送哪家近」。

### 锁定状态生命周期

```text
   ┌──────────┐
   │ 未锁定    │  ← 用户从未指定 shop_id（普通入口）
   └────┬─────┘
        │ 调用 /chat 时 body 里有 shop_id
        ▼
   ┌──────────────────┐
   │ 锁定态（会话级）   │  ← 由 mem_store.get_session_shop_id 持久化
   └────┬─────────────┘
        │ 整轮不变（同一 session_id）
        ▼
   工具调用、DIY 方案、下单全程携带该 shop_id
```

### 4 层联动约束（运维必读）

| 层 | 文件 / 工具 | 约束内容 |
|----|------------|---------|
| ① 数据层 | `backend/data_gateway/external.py::query_external_entity` | 映射含 `shop_id` 列时用 `CAST("col" AS text) = %s` 强制 WHERE 过滤；无该列不过滤 |
| ② 工具层 | `agent/data_tools.py::platform_db_query_entity` | 自动从 `_context.shop_id` 注入；漏传自动套用；**传别店 shop_id 一律拒绝**；返回 `meta.shop_scoped` 让 LLM 知道已过滤 |
| ③ DIY 层 | `agent/diy_tools.py::{generate,revise,design}_diy_plan` | 提示词注入「只能选用该店在售花材，不确定先查 platform_db_query_entity」；方案写 `shop_id`；改版继承原方案店铺范围 |
| ④ 下单层 | `agent/skills/skill_order.py::create_order` | 锁定时自动填充 `shop_id`（`first` 占位）；传别店被拒；方案归属 `plan['shop_id']` 与锁定不一致被拒 |

### 锁定 vs 未锁定的行为差异

| 行为 | 未锁定 | 锁定 |
|------|-------|------|
| 询问「去哪家店」 | 是 | 否 |
| 推 `shop_card` | 是 | 否 |
| DIY 选材范围 | 知识库全部 | 仅该店在售（平台库验证） |
| 下单 `shop_id` | 用户确认 / 选第一项 | 自动用锁定店铺 |
| 跨店方案 | 允许 | 拒绝 |
| 缺货时 | 推其他店 | 给本店替代方案，不拿别家凑 |

### 接入方配合项

- **前端**：从店铺页入口调用 `/chat` 时把 `shop_id` 放进 body；可选项不要传（避免误锁定）
- **平台方**：必须映射 `plan.shop_id` 和 `order.shop_id`（参见 §B 业务数据源 B4 表）；不映射的话锁定退化为「模型按返回行内字段自行筛」（不可靠）
- **退出锁定**：暂无显式接口；如需让用户切到其他店铺，重建会话（`POST /conversations` 新建）

详细行为表 + 触发 JSON 示例见 [接入说明 §4.6](接入说明-花艺智能体API.md)。

---

## 🔌 平台接入契约

> ⚠️ **前端渲染由宿主平台负责，本包不携带前端。** 智能体后端只产出结构化 `ui / data / action`；接入平台**必须先实现对应的前端组件**（`text` / `dialog_options` / `plan_card` / `shop_card` / `order_card` / `pay_jump` / `image_task` / **`greeting_card`**），否则会出现「有数据无展示」。每个 UI 类型的数据结构、渲染要求与示例请阅读 **[FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)**，或调用 `GET /ui-contract` 拉取机器可读清单对照。

`POST /chat` 的响应保留兼容字段 `reply`、`ui`、`data`，并增加平台中立的 `action`：

```json
{
  "reply": "我为你推荐了一个生日花束方案",
  "ui": "plan_card",
  "data": { "plans": [{ "plan_id": "P001", "name": "生日玫瑰花束", "price": 199 }] },
  "action": {
    "type": "show_plan",
    "payload": { "ui": "plan_card", "data": { "plans": [...] }, "stage": "plan_confirm" },
    "required_capabilities": ["show_plan_page"],
    "fallback": "当前平台暂不支持方案页面，请先展示文本和方案数据。"
  },
  "session_id": "会话 ID",
  "stage": "plan_confirm"
}
```

### UI 类型与对应数据

| `ui` | 用途 | 典型场景 |
|------|------|----------|
| `text` | 文本气泡 | 知识问答、状态说明 |
| `dialog_options` | 选项按钮 | 模式选择（现成方案 / DIY）、确认 |
| `plan_card` | 方案卡片 | 在售花束 / DIY 方案展示 + 确认 |
| `shop_card` | 店铺卡片 | 推荐可配送店铺（未锁定模式下） |
| `order_card` | 订单确认 | 用户确认订单方案 |
| `pay_jump` | 支付跳转 | 打开平台支付页（含 `pay_url` 或 `pay` 对象） |
| `image_task` | 生图进度 | DIY 效果预览（按 `task_id` 轮询 `GET /tasks/{id}`） |
| `greeting_card` | 电子贺卡 | 下单后生成的竖版贺卡（`/generated/greet_*.png`） |

### 平台侧能力要求

| 能力 | 平台侧职责 |
|---|---|
| `show_plan_page` | 展示方案名称、花材、价格、图片和确认/修改按钮 |
| `show_shop_page` | 展示店铺、距离、评分、配送和选择按钮 |
| `show_options` | 展示模式选择、确认和修改选项 |
| `start_image_task` | 展示生成中状态，按任务 ID 轮询图片 |
| `create_order` | 在用户确认后调用订单服务并展示订单详情 |
| `render_greeting_card` | 渲染电子贺卡图（含/不含可点开大图） |
| `open_payment` | 使用平台支付能力打开支付页面 |

不支持某个 action 时，平台应使用 `fallback` 或 `reply` 继续对话，不要直接报错中断。`required_capabilities` 只表示接入方需要具备的能力，不是智能体可以自行调用的前端接口。

---

## 前端对接契约

**接入前必读：[FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)** —— 该文档把每个 UI 类型（8 种）的数据结构、渲染要求、示例 payload 和「接入前 Check 清单」写清楚。接入的开发者或 AI 在开始对接时读一遍，就能知道自己要补哪些前端组件，避免「有数据无展示」。

后端同时提供 `GET /ui-contract`，返回与文档一致的机器可读清单，方便前端/AI 在接入脚本里自动拉取、逐项对照。

关键提醒（三点）：

1. 本包是纯后端 API，**前端由宿主平台自研**；
2. 若宿主前端缺 `plan_card` / `shop_card` / `pay_jump` / `image_task` / `greeting_card` 等组件，接上后体验是坏的 —— 请先补齐再联调；
3. 暂缺的组件先用文本降级（渲染 `reply` + `action.fallback`），不要报错中断对话。

---

## 💌 AI 电子贺卡

下单后可引导用户一键生成电子贺卡，搭配订单送出去。流程由两个工具协作完成：

```text
                  用户输入「送给妈妈生日」
                              │
                  suggest_greetings  ────────►  候选情话清单（recipient × occasion × style 命中 + 兜底）
                              │
                          用户挑一句（或让模型挑）
                              │
                  render_greeting_card(text, recipient, sender, template)
                              │
                              ▼
                       /generated/greet_*.png
                              ▼
                            greeting_card UI
```

### 工具

| 工具 | 作用 |
|------|------|
| `suggest_greetings(recipient, occasion, style)` | 在 47 条情景词库中按收卡人 × 场合 × 风格精匹配 + 兜底，输出候选清单 |
| `render_greeting_card(text, recipient, sender, template?)` | 用 Pillow 在容器内**模板合成** 900×1200 竖版图（不调外部 provider） |

### 模板（共 5 套）

| template | 风格 | 适用 |
|----------|------|------|
| `blush`（默认） | 粉色浪漫 | 妈妈、恋人 |
| `warm` | 奶油金边 | 老师、长辈 |
| `green` | 自然清新 | 朋友、客户 |
| `letter` | 信笺风 | 老师、长辈（手写感） |
| `night` | 夜色氛围 | 道歉、深夜祝福 |

### 字体降级链

`CARD_FONT_PATH` → `/usr/share/fonts/truetype/wqy/wqy-microhei.ttc`（容器内） → `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` → Windows `msyh.ttc` → macOS `PingFang.ttc`。Dockerfile 装的是文泉驿微米黑（约 5MB）+ 阿里云镜像源；老的 `fonts-noto-cjk`（300MB+）因 apt 卡死已弃用。

### 返回结构

```json
{
  "image_url": "/generated/greet_xxx.png",
  "text": "妈妈，您是我心里永远的春天。",
  "recipient": "妈妈",
  "sender": "小明",
  "template": "blush",
  "note": null
}
```

---

## 🧭 Vibe Coding 快速理解

如果你第一次接触本项目，按下面的方式理解：

- `main.py`：FastAPI 服务入口（title=`跳舞兰花卉智能体 API`）
- `agent/agent.py`：`Agent` 类，主循环（ReAct 工具循环、回复、结果编排、店铺锁定分支）
- `agent/toolkit.py`：工具注册表（`@register_tool`）+ `execute_tool` 上下文注入 + `MCP_SAFE_TOOL_NAMES` 白名单
- `agent/tools.py`：花艺核心能力、需求抽取、知识检索；包含 `show_plan_card` 终结工具
- `agent/data_tools.py`：平台只读连接器（`platform_db_*`）+ 映射版本工具（`platform_mapping_*`）
- `agent/diy_tools.py`：DIY 方案、改版、生图任务（`generate_diy_plan` / `revise_diy_plan` / `generate_effect_image`）
- `agent/memory_tools.py`：用户级 / 会话级记忆与偏好读写
- `agent/skills/skill_order.py`：下单编排、金额核算、支付跳转参数；锁定模式自动填 `shop_id`，跨店拒绝
- `agent/skills/skill_greeting.py`：**AI 贺卡双工具**（`suggest_greetings` 47 词库 + `render_greeting_card` 5 模板）
- `agent/engine/ui_protocol.py`：`UIType`、`ChatResponse`、`AgentAction`、`GreetingCard` 数据契约
- `agent/engine/state.py`：会话状态机 + 店铺锁定状态
- `agent/engine/budget.py` / `circuit_breaker.py`：单轮 / 单用户预算熔断
- `agent/ports.py`：跨模块 Protocol 契约
- `domain/requirements.py`：跨模块共享的 `FlowerRequirement`
- `backend/storage/`：内部 PostgreSQL 运行时表（sessions/messages/memories/image_tasks/mapping_drafts/mapping_audit/diy_plans/notifications/operations_config 等）
- `backend/data_gateway/`：平台只读连接器 + 实体映射存储
- `backend/routers/chat.py`：平台调用的 HTTP/SSE 接口
- `agent/knowledge/*.json`：花材、风格、场景、搭配、预算和包装知识

### 修改时三条原则

1. 新能力优先实现为独立工具，通过 `@register_tool` 注册，不把业务逻辑继续堆进 `agent.py`。
2. 平台差异放在 `backend/data_gateway/`、mapper、平台适配层，智能体只依赖标准字段和 action 协议。
3. 任何会改变订单、支付或用户数据的操作都必须有权限、确认、幂等和失败回滚策略。

### 推荐 Vibe Coding 指令

```text
请先阅读 README、DEPLOY、FRONTEND_CONTRACT、接入说明-花艺智能体API.md、
agent/engine/ui_protocol.py、agent/ports.py、agent/toolkit.py、agent/skills/skill_greeting.py，
理解「需求理解 → 平台只读检索 → 方案卡片 → 预览图 → 店铺（含锁定） → 订单 → 支付申请 → 可选电子贺卡」
的完整链路。
修改时保持 ChatResponse 的兼容字段和 AgentAction 协议不变；先检查现有工具注册，避免重复注册；
不要让 LLM 直接执行支付，真实支付由宿主平台和后端业务服务承接；
店铺锁定模式下不要擅自换店，不要让 LLM 选花材绕开 platform_db_query_entity。
```

---

## 📚 知识库管理

```
agent/knowledge/
├── flowers.json        # 花材信息（名称、花语、养护、季节）
├── styles.json         # 花艺风格（现代、自然、复古）
├── scenes.json         # 应用场景（婚礼、生日、节日）
├── pairings.json       # 搭配建议
├── budget.json         # 预算方案
├── packaging.json      # 包装建议
├── sources/            # 扩展知识源（标准化后暂存）
└── store.py            # 知识库加载器
```

### 知识条目格式

```json
{
  "id": "F_ROSE_001",
  "name": "玫瑰",
  "aliases": ["红玫瑰", "粉玫瑰", "白玫瑰"],
  "tags": ["主花", "浪漫", "爱情"],
  "source": "core",
  "flower_language": ["爱情", "浪漫", "美丽"],
  "care": { "light": "充足散射光", "water": "见干见湿", "temperature": "15-25°C" },
  "season": ["四季"],
  "colors": ["红", "粉", "白", "黄", "紫"],
  "pairing_notes": "适合搭配满天星、尤加利、洋桔梗",
  "price_range": { "low": 2, "high": 15, "unit": "支" }
}
```

### 添加新知识

1. 编辑 `agent/knowledge/` 下对应 JSON 文件
2. 按格式补 `id` / `name` / `aliases` / `tags` / `source` 等字段
3. 重启服务生效

### 导入外部知识

```bash
python scripts/import_flower_knowledge.py
```

支持的数据源：`PlantFlowerDatasets` / `flower-db` / `Flower-Knowledge-Graph-Visualization` / `flora-atlas`。

---

## 🚢 部署指南

### Docker 生产部署（推荐）

```bash
# 1. 准备配置
cp .env.example .env
vim .env  # 填入生产配置（LLM / JWT_SECRET / PLATFORM_API_KEYS / POSTGRES_PASSWORD 等）

# 2. 构建并启动（自带 PostgreSQL）
docker compose up -d --build

# 3. 可选：启用 Nginx 反向代理（HTTPS + SSE）
#    阿里云免费证书放到 deploy/certs/{fullchain,privkey}.pem，修改 deploy/nginx.conf 中的 server_name
docker compose --profile nginx up -d

# 4. 设置开机自启
docker update --restart unless-stopped flora-agent

# 5. 查看状态
docker compose ps
docker compose logs -f agent
```

**三服务编排**：

- `postgres`（pgdata 卷数据持久化、有 `healthcheck`）
- `agent`（依赖 `service_healthy`，等 PG 起来后才启）
- `nginx`（仅 `--profile nginx` 启用，反向代理 + 托管 `/generated` 静态图）

> **关键提醒**：Dockerfile 用 `COPY . .` 把代码烤进镜像，agent 容器只 bind-mount `./data`。**改代码后必须 `docker compose up -d --build agent` 重建镜像**，光 restart 无效。

### 字体与镜像源

Dockerfile 已切换阿里云镜像源 + 装 `fonts-wqy-microhei`（约 5MB），旧的 `fonts-noto-cjk`（300MB+）因 apt 卡死已弃用。该字体服务于 AI 贺卡的模板合成。如需自定义：

```dockerfile
# 在 Dockerfile 内追加字体 COPY 行，重新构建
COPY ./assets/your-font.ttf /usr/share/fonts/custom/
RUN fc-cache -fv
```

并在 `.env` 配 `CARD_FONT_PATH=/usr/share/fonts/custom/your-font.ttf`。

### Nginx 反向代理配置

参考 `deploy/nginx.conf`，关键配置：

```nginx
location / {
    proxy_pass http://agent:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # SSE 关键
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;
}
```

### systemd 服务（裸机部署）

```bash
sudo tee /etc/systemd/system/flora-agent.service << EOF
[Unit]
Description=Flora Agent Service
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/flora_agent_package
ExecStart=/opt/flora_agent_package/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable flora-agent
sudo systemctl start flora-agent
sudo systemctl status flora-agent
```

完整部署细节（openclaw 镜像定制 / 防火墙规则 / 阿里云免费证书 / 续期 / 排障）见 [DEPLOY.md](DEPLOY.md)。

---

## ❓ 常见问题

### Q: LLM 调用报错 401？

**A**: 检查 `LLM_API_KEY` 是否正确、是否过期、是否有额度。Docker 内若连外网失败，先确认基础镜像源是否切阿里云（`apt` 卡死通常是没切源）。

### Q: 文生图不生成？

**A**:
- 当前代码仅 `mock` + `hy` 两个 provider 实现；其他 provider 写入 `.env` 仍走 mock
- 检查 `IMAGE_API_KEY` / `IMAGE_BASE_URL` / `IMAGE_MODEL` 是否按所选 provider 填写
- 默认 `IMAGE_PROVIDER=mock` 不产生真实图片，只能在 `data/generated/` 留占位文件

### Q: AI 贺卡不渲染中文 / 显示方块？

**A**: 容器内缺中文字体。Docker 部署应该没问题（已装 `fonts-wqy-microhei`）；如用裸机部署：

```bash
apt-get install fonts-wqy-microhei   # Debian/Ubuntu
# 或在 .env 配 CARD_FONT_PATH 指向本机字体路径
```

裸机字体探测路径见 [§C2](#c2-ai-电子贺卡)。症状是 GIF/PIL 渲染图里中文变 □，调大字体降级链或装系统字体包。

### Q: 微信登录失败？

**A**: 检查 `WECHAT_APPID` 和 `WECHAT_SECRET` 是否与小程序后台一致（旧名 `WX_APPID` / `WX_SECRET` 也兼容）；小程序是否已发布或开启了开发版体验。

### Q: `POST /chat` 返回 401 / 403？

**A**:
- 401：未带或 token 过期，重走 `/auth/token` 换
- 403：请求体 `user_id` 与 `/auth/token` 返回的派生 `user_id`（形如 `wxmini_<hex>`）不一致；**生产环境 token 与 user_id 必须成对出现**

### Q: 数据库连接失败？

**A**:
- `DATABASE_URL` 使用 `postgresql://`，检查地址 / 端口 / 账号 / 密码 / 网络白名单
- 驱动 `psycopg[binary]` 已写入 `requirements.txt`
- **生产环境不允许 SQLite**
- Docker 启动顺序：等 postgres healthy 后 agent 才会启

### Q: 平台只读库未配置 / 查不到商品？

**A**:
- 必须填 `PLATFORM_DB_<SOURCE_ID>_URL`（如 `PLATFORM_DB_MAIN_URL`）
- 必须完成 [§B 业务数据源 B3](#b-必填业务数据源按接入说明-2-流程) 的 7 步接入流程、激活 mapping
- 没 active mapping 时 LLM 会如实告知「未接入」——这是设计如此，不要以为它在编

### Q: 下单接口返回错误？

**A**:
- 必须配 `PLATFORM_ORDER_API_URL`（可选 `PLATFORM_ORDER_API_KEY`），否则 `create_order` 直接报错
- 锁定模式下「跨店拒绝」、方案归属校验失败也会拒绝下单
- 错误信息会通过 `order_card.data.error` 回灌前端

### Q: 店铺锁定模式下为何不能切店？

**A**: 设计如此。会话一旦带 `shop_id` 进入，整轮会话都锁定在该店；如需切店，**重建会话**（前端调 `POST /conversations` 新建，前端再带新 `shop_id` 调 `/chat`）。

### Q: 小程序请求被 CORS 拦截？

**A**: 检查 `ALLOWED_ORIGINS` 是否包含小程序域名（不含路径），多个域名用逗号分隔；小程序上线后建议配置为具体域名（不写 `*`）。

### Q: 工具调用超时？

**A**: 调大 `LLM_REQUEST_TIMEOUT`（默认 120 秒）和代理层 `proxy_read_timeout`（默认 300 秒）。SSE 长连接必须开 `proxy_buffering off;`。

### Q: 如何添加新工具？

**A**:
1. 在 `agent/` 下新建模块（如 `agent/my_tools.py`）
2. 用 `@register_tool(name=..., inject_context=True/False, description=...)` 装饰
3. 在 `agent/__init__.py` 加一行 `from . import my_tools`（**否则工具不会被注册！**）
4. 重建 agent 镜像：`docker compose up -d --build agent`

### Q: 如何查看智能体思考过程？

**A**: 使用 `/chat/stream` SSE 接口，监听 `tool_call` 事件；前端自己拼 `text` 增量片段。

### Q: 怎样对接 image_task / greeting_card？

**A**:
- 拿到 `data.task_id` 后轮询 `GET /tasks/{task_id}`，直到 `status=done`，`result_url` 即图片地址
- 贺卡图直接走 `GET /generated/{filename}.png`，无需轮询（推送时已 `done`）

### Q: 怎样让 LLM 看到「店铺不存在」时如实告知而不是瞎编？

**A**: 已经按设计实现——平台数据源未配置、active mapping 缺失、锁定店铺无在售商品，三种情况 LLM 都会主动说明「平台未接入 / 暂无数据」，不会编造商品名或价格。

---

## 📄 许可证

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。
