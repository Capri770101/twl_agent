# 🌸 Flora Agent - 花艺智能体

基于 ReAct 架构的花艺顾问 AI 系统，支持微信小程序接入，提供专业的花艺设计建议和效果图像生成。

## 📋 目录

- [项目特性](#项目特性)
- [系统架构](#系统架构)
- [快速开始](#快速开始)
- [环境配置](#环境配置)
- [API 接口文档](#api-接口文档)
- [部署前必须配置的内容](#部署前必须配置的内容)
- [完整业务流程](#完整业务流程)
- [平台接入契约](#平台接入契约)
- [前端对接契约](#前端对接契约-frontend-contract)
- [Vibe Coding 快速理解](#vibe-coding-快速理解)
- [知识库管理](#知识库管理)
- [部署指南](#部署指南)
- [常见问题](#常见问题)

## 🌟 项目特性

- **ReAct 智能体架构**：基于推理-行动循环的智能对话系统
- **多轮对话支持**：维护会话上下文，提供连贯的花艺建议
- **知识库驱动**：内置丰富的花艺知识（花材、风格、场景、搭配等）
- **效果图像生成**：支持多种文生图模型生成花艺效果图
- **微信小程序原生支持**：完整的登录、对话、会话管理接口
- **SSE 流式响应**：实时返回智能体思考过程和回复
- **平台中立动作协议**：支持把方案展示、订单创建、支付跳转抽象成统一 action，方便独立部署到不同平台

## 🏗️ 系统架构

```
flora_agent_package/
├── main.py                    # FastAPI 服务入口
├── agent/                     # 智能体核心模块
│   ├── agent.py              # ReAct 智能体实现
│   ├── tools.py              # 花艺核心工具
│   ├── data_tools.py         # 文件/数据库/外部数据源工具
│   ├── diy_tools.py          # DIY 方案与生图工具
│   ├── shop_tools.py         # 店铺推荐与花材匹配
│   ├── skills/               # 技能模块
│   ├── engine/               # 前端协议/动作协议
│   └── knowledge/            # 知识库
├── backend/                   # 后端服务
│   ├── config.py             # 配置管理
│   ├── routers/              # API 路由
│   ├── storage/              # 内部 PostgreSQL 存储
│   └── data_gateway/         # 内外部数据网关与映射
├── scripts/                   # 脚本工具
├── .env.example               # 环境变量模板
├── requirements.txt           # Python 依赖
├── Dockerfile                 # Docker 镜像
├── docker-compose.yml         # Docker 编排
└── DEPLOY.md                  # 部署详细文档
```

## 🚀 快速开始

### 方式一：Docker 部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/your-username/flora_agent_package.git
cd flora_agent_package

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入必要的配置

# 3. 启动服务
docker-compose up -d

# 4. 查看日志
docker-compose logs -f agent

# 5. 验证服务
curl http://localhost:8000/health
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
# 编辑 .env 文件

# 4. 启动服务
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 5. 本地测试
python scripts/test_agent_local.py
```

## ⚙️ 环境配置

### 必填配置（二选一）

**方式一：OpenAI 兼容接口**

| 变量 | 说明 | 示例 |
|------|------|------|
| `LLM_API_KEY` | LLM API 密钥 | `sk-xxx` |
| `LLM_BASE_URL` | LLM API 地址 | `https://api.openai.com/v1` |
| `LLM_MODEL` | 模型名称 | `gpt-4o-mini` |

**方式二：hy 大模型（推荐）**

| 变量 | 说明 | 示例 |
|------|------|------|
| `HY_API_KEY` | hy 大模型 API 密钥 | `sk-xxx` |
| `HY_BASE_URL` | hy 大模型 API 地址 | `https://tokenhub.tencentmaas.com/v1/responses` |
| `HY_LLM_MODEL` | LLM 模型名称 | `hy3` |
| `HY_IMAGE_MODEL` | 图像生成模型名称 | `Hy-Image-3.0` |

### 可选配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `APP_ENV` | 运行环境 | `prod` |
| `PORT` | 服务端口 | `8000` |
| `DATABASE_URL` | 智能体内部 PostgreSQL 连接串；生产环境禁止 SQLite | - |
| `IMAGE_PROVIDER` | 图像生成提供商 | `mock` |
| `IMAGE_PUBLIC_BASE_URL` | 生图结果公网前缀（CDN/对象存储域名），留空用本服务 `/generated` | - |
| `WECHAT_APPID` | 微信小程序 AppID（兼容 `WX_APPID`） | - |
| `WECHAT_SECRET` | 微信小程序 AppSecret（兼容 `WX_SECRET`） | - |
| `TENCENT_MAP_KEY` | 腾讯地图密钥 | - |
| `REDIS_URL` | Redis 连接串 | - |
| `ALLOWED_ORIGINS` | CORS 允许域名 | `*` |

> 外部目标平台数据库不在 `.env.example` 中固定声明，而是按数据源单独配置 `PLATFORM_DB_<SOURCE_ID>_URL`。例如 `PLATFORM_DB_MAIN_URL` 用于 `source_id=main` 的只读连接。

### 支持的 LLM 提供商

| 提供商 | Base URL | 模型示例 |
|--------|----------|----------|
| hy 大模型（推荐） | `https://tokenhub.tencentmaas.com/v1/responses` | `hy3` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b` |

### 支持的图像生成提供商

| 提供商 | Base URL | 模型示例 |
|--------|----------|----------|
| mock | 本地内置 | - |
| hy 大模型（推荐） | `https://tokenhub.tencentmaas.com/v1/responses` | `Hy-Image-3.0` |

> 注：当前代码仅实现 `mock` 与 `hy` 两个 provider；其他 provider 为预留声明，接入前需补充对应实现，否则仍会走 mock。

**生图结果存储**：PNG 写入本地 `data/generated/{task_id}.png`，经本服务 `GET /generated/{task_id}.png` 访问；`/chat` 的 `image_task` 数据里 `result_url` 即该地址。若生产环境把图片托管到 CDN/对象存储，配置 `IMAGE_PUBLIC_BASE_URL=https://你的域名`，`result_url` 会自动带上公网前缀。

**任务状态持久化**：任务状态写入 PostgreSQL 的 `image_tasks` 表，不再依赖进程内存，因此服务重启后已完成任务仍可通过 `GET /tasks/{task_id}` 查询；服务重启时遗留的 `processing` 任务会标记为 `failed`，避免客户端无限轮询。图片文件本身仍需由本地磁盘或对象存储/CDN负责保留。

如果部署方数据库中没有 `image_tasks` 表：有 DDL 权限时启动会自动创建；没有 DDL 权限时由 DBA 先执行 [`migrations/001_image_tasks.sql`](migrations/001_image_tasks.sql)。服务启动自检会验证该表可查询，缺表或权限不足会直接失败并给出迁移提示。

完整配置请参考 [.env.example](.env.example) 和 [DEPLOY.md](DEPLOY.md)。

## 📡 API 接口文档

### 基础信息

- **Base URL**: `http://localhost:8000`
- **Content-Type**: `application/json`
- **认证方式**: 生产环境使用 `Authorization: Bearer <access_token>`；开发环境可通过 `AUTH_REQUIRED=false` 关闭

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
POST /auth/token        # 通用平台接入（X-API-Key + external_user_id 换 token）
POST /auth/anonymous    # 匿名登录（仅开发联调；生产默认关闭）
POST /auth/wx-login     # 微信小程序（code 换 token）
GET /auth/me            # 当前用户信息
```

#### 多平台接入（推荐路径）

智能体独立部署后，各平台统一按「平台 API Key + 用户标识」接入：

1. 部署方在 `.env` 的 `PLATFORM_API_KEYS` 中为每个接入方配置 `platform_id=key`；
2. 接入方后端先认证自己的终端用户（小程序/H5/App 各用各的登录体系）；
3. 接入方后端携带 `X-API-Key` 调用 `POST /auth/token`，提交该用户在接入方体系内的 `external_user_id`，换取智能体 JWT；
4. 之后前端/接入方后端用该 JWT 调用 `/chat`、`/conversations` 等业务接口。

```text
接入方前端 ──登录──> 接入方后端 ──X-API-Key + external_user_id──> POST /auth/token
                <──────────── 智能体 access_token ────────────
接入方前端 ──Bearer token──> 智能体 /chat、/chat/stream、/conversations ...
```

**信任模型**：持有 API Key 的一方负责认证终端用户；智能体侧的 `user_id` 由 `platform_id + external_user_id` 哈希派生，天然隔离各平台用户、不落盘原始标识，Key 轮换不影响用户身份稳定性。

微信小程序也可以走同一条路（后端换 token），或使用内置的 `/auth/wx-login`（配置 `WECHAT_APPID`、`WECHAT_SECRET`，兼容旧名称 `WX_APPID`、`WX_SECRET`，由智能体直接调微信 `jscode2session`）。

`/auth/anonymous` 仅用于开发和联调：生产环境（`APP_ENV=prod`）未显式配置 `ANONYMOUS_LOGIN_ENABLED=true` 时自动禁用，避免匿名接口被刷 token 消耗模型额度。生产请求必须携带 Bearer Token，且 token 中的用户必须与请求 `user_id` 一致。

当前仓库没有 MCP 全量工具桥接；如果宿主自行接入 MCP，只能使用 `agent.toolkit.get_mcp_tool_specs()` 的显式白名单。文件系统、数据库发现和外部数据库工具不得无条件暴露。MCP 通道只输出文本、图片 URL 和平台链接，不直接渲染 `ui/data` 卡片。

### 前端契约查询

```
GET /ui-contract
```

返回全量 UI 类型清单（数据字段 + 渲染要求 + 示例 payload），供前端 / AI 在接入时**程序化对照自己实现了哪些组件**。示例：

```json
{
  "ui_types": [
    { "ui": "plan_card", "action_type": "show_plan", "required_capabilities": ["show_plan_page"], "render": "方案卡片……", "example": { "plans": [{ "plan_id": "P001", "name": "生日玫瑰花束", "price": 199 }] } }
  ]
}
```

完整数据契约见 [FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)。

### 对话接口

```text
POST /chat
POST /chat/stream
```

请求体包含：`message`、`user_id`、`session_id?`、`location?`、`shop_id?`。生产环境由宿主平台完成用户登录，后端要求 `user_id` 与 token 一致。

`/chat` 返回当前代码中的标准结构：`reply`、`ui`、`data`、`action`、`tool_calls`、`session_id`、`stage`；其中 `ui` 由 `UIType` 决定，`action` 由 `AgentAction` 决定。

`/chat/stream` 使用 SSE，常见事件为 `thinking`、`tool_call`、`tool_result`、`text`、`error`。

| 事件 | 说明 |
|------|------|
| `thinking` | 智能体思考过程 |
| `text` | 文本回复内容 |
| `tool_call` | 工具调用状态 |
| `image` | 生成的图片链接 |
| `card` | UI 卡片数据 |
| `error` | 错误信息 |
| `done` | 对话完成 |

### 会话管理

#### 获取会话列表

```
GET /conversations?user_id=user_123456
```

**响应示例**：
```json
[
  {
    "id": "conv_xxxx",
    "title": "给女朋友送花",
    "preview": "推荐玫瑰和满天星搭配...",
    "created_at": "2024-01-01T00:00:00",
    "shop_id": "shop_789"
  }
]
```

#### 获取会话消息

```
GET /conversations/{conversation_id}/messages?limit=50
```

**响应示例**：
```json
[
  {
    "role": "user",
    "content": "我想给女朋友送花",
    "timestamp": "2024-01-01T00:00:00"
  },
  {
    "role": "assistant",
    "content": "根据您的需求...",
    "timestamp": "2024-01-01T00:00:01"
  }
]
```

#### 创建会话

```
POST /conversations
```

**请求体**：
```json
{
  "user_id": "user_123456",
  "title": "生日礼物咨询",
  "shop_id": "shop_789"
}
```

**响应示例**：
```json
{
  "conversation_id": "conv_xxxx",
  "id": "conv_xxxx"
}
```

#### 重置会话

```
POST /chat/reset
```

**请求体**：
```json
{
  "user_id": "user_123456",
  "session_id": "conv_xxxx"  // 可选，不传则删除最新会话
}
```

**响应示例**：
```json
{
  "ok": true,
  "session_id": "conv_xxxx"
}
```

### 图像任务查询

```
GET /tasks/{task_id}
```

**响应示例**：
```json
{
  "task_id": "task_xxxx",
  "status": "done",
  "result_url": "https://...",
  "created_at": "2024-01-01T00:00:00",
  "updated_at": "2024-01-01T00:00:30"
}
```

## 📱 微信小程序接入

### 1. 登录流程

```javascript
// app.js 或登录页面
wx.login({
  success: async (res) => {
    if (res.code) {
      // 发送 code 到后端换取 token
      const { data } = await wx.request({
        url: 'https://your-domain.com/auth/wx-login',
        method: 'POST',
        data: { code: res.code }
      });
      
      // 存储 token
      wx.setStorageSync('token', data.token);
      wx.setStorageSync('user_id', data.user_id);
    }
  }
});
```

### 2. 发送消息

```javascript
// chat.js
async function sendMessage(message, conversationId, shopId) {
  const userId = wx.getStorageSync('user_id');
  
  const { data } = await wx.request({
    url: 'https://your-domain.com/chat',
    method: 'POST',
    header: {
      'Content-Type': 'application/json'
    },
    data: {
      message: message,
      user_id: userId,
      session_id: conversationId,
      shop_id: shopId
    }
  });
  
  return data;
}
```

### 3. SSE 流式对话

```javascript
// 使用 WebSocket 或 EventSource 处理 SSE
function chatStream(message, conversationId, shopId) {
  const token = wx.getStorageSync('token');
  
  // 微信小程序不支持原生 EventSource，需要使用 WebSocket
  const task = wx.connectSocket({
    url: `wss://your-domain.com/chat/stream?token=${token}`,
    method: 'POST',
    data: {
      message: message,
      user_id: wx.getStorageSync('user_id'),
      session_id: conversationId,
      shop_id: shopId
    }
  });
  
  task.onMessage(function(res) {
    // 解析 SSE 事件
    const events = res.data.split('\n\n');
    events.forEach(event => {
      if (event.startsWith('event:')) {
        const [eventType, ...dataLines] = event.split('\n');
        const data = JSON.parse(dataLines[0].replace('data: ', ''));
        
        switch (eventType.replace('event: ', '')) {
          case 'text':
            // 处理文本回复
            break;
          case 'image':
            // 处理图片
            break;
          case 'done':
            // 对话完成
            break;
        }
      }
    });
  });
}
```

### 4. 获取会话列表

```javascript
async function getConversations() {
  const userId = wx.getStorageSync('user_id');
  
  const { data } = await wx.request({
    url: `https://your-domain.com/conversations?user_id=${userId}`,
    method: 'GET'
  });
  
  return data;
}
```

### 5. 获取历史消息

```javascript
async function getMessages(conversationId) {
  const { data } = await wx.request({
    url: `https://your-domain.com/conversations/${conversationId}/messages`,
    method: 'GET'
  });
  
  return data;
}
```

## 🚀 部署前必须配置的内容

部署前请按以下顺序完成配置。`.env.example` 只提供模板，不要把真实密钥提交到代码仓库。

### A. 必填：模型服务

至少配置一套 OpenAI 兼容的模型服务：

```env
LLM_API_KEY=你的模型密钥
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

模型必须支持 Chat Completions 和 function/tool calling。若使用 DeepSeek、通义千问、Ollama 或其他兼容服务，只需替换 `LLM_BASE_URL`、`LLM_MODEL` 和密钥。此前出现的 `503 model_not_found / Waiting for service resources` 属于模型服务资源未就绪，应检查模型名和服务商状态，并配置重试/备用服务。

### B. 必填：业务数据源

**生产环境禁止使用 SQLite**。SQLite 是单文件数据库，不适合作为本服务的生产并发读写存储。生产必须配置 PostgreSQL（或兼容 PostgreSQL 协议的托管数据库）：

```env
DATABASE_URL=postgresql://用户名:密码@数据库地址:5432/数据库名
```

`DB_PATH` 仅用于历史开发配置，不应在生产环境使用。部署时应准备独立的 PostgreSQL 数据库、连接池、备份、迁移和监控。

目标平台数据库与智能体内部数据库必须分离。内部 `DATABASE_URL` 只保存会话、记忆、任务和智能体自身数据；外部平台库通过服务端环境变量 `PLATFORM_DB_<SOURCE_ID>_URL` 连接，默认使用只读账号，LLM 不会接收连接串。

可先调用 `platform_db_test_connection(source_id)` 验证网络、账号和数据库类型，再调用 `platform_db_discover(source_id, sample_rows=0)` 获取元数据；只有在字段语义仍不明确时，才调用 `platform_db_sample_table` 获取指定表的最多 5 行脱敏样本。工具不接受任意连接串或任意 SQL，样本也不会默认读取。

目标平台已有商品、店铺、订单数据时，智能体通过数据网关读取标准业务实体。至少需要映射：

- `plan`：花束/方案/商品，至少包含 ID、名称、价格、描述、图片、标签
- `shop`：店铺，至少包含 ID、名称、地址、评分、配送信息
- `order`：订单，至少包含订单 ID、用户 ID、方案 ID、金额、状态、创建时间
- 可选 `order_items`：订单明细

如果目标平台字段名不同，使用 `data_mapping.json` 指定实际表和字段。自动推断只适用于有语义的表名/字段名；对于 `t1`、`a`、`b`、`c` 等无意义命名，不能仅凭名字可靠推断。

推荐让熟悉目标数据库的 AI 辅助接入：先运行 `db_discover` 获取表结构和少量脱敏样本，再让 AI 根据平台数据字典、字段注释和样本生成 `data_mapping.json` 草案；平台方必须审核映射、确认读写范围后，才允许开启订单写入。流程是：

```text
db_discover → AI 生成映射草案 → 人工审核 → write_enabled=true → 强制刷新映射 → 只读联调 → 订单写入联调
```

AI 可以辅助理解无意义字段，但不能凭空保证字段含义；没有字段注释、样本或数据字典时，必须由平台方确认。订单写入必须显式配置 `write_enabled=true`，避免智能体误写未知数据库。

### C. 按需配置：图像生成

```env
IMAGE_PROVIDER=mock
IMAGE_API_KEY=
IMAGE_BASE_URL=
IMAGE_MODEL=
```

- `mock`：适合开发和接口联调，不产生真实图片
- 真实 provider：必须填写 API Key、Base URL 和 Model
- 生图接口返回任务 ID 后，平台通过 `GET /tasks/{task_id}` 轮询状态

### D. 按需配置：平台基础设施

```env
APP_ENV=prod
HOST=0.0.0.0
PORT=8000
ALLOWED_ORIGINS=https://你的平台域名
REDIS_URL=                 # 可选：限流、缓存和预算能力
TENCENT_MAP_KEY=           # 可选：位置与距离能力
WECHAT_APPID=              # 仅微信小程序需要（兼容 WX_APPID）
WECHAT_SECRET=              # 仅微信小程序需要（兼容 WX_SECRET）
```

部署方还需要准备：HTTPS 域名、反向代理、数据库备份、日志采集、模型服务额度，以及目标平台的订单和支付凭据。支付密钥、用户身份认证和支付回调不能写入智能体代码。

### E. 启动与自检

```bash
pip install -r requirements.txt
python -m compileall -q agent backend domain main.py
python -m uvicorn main:app --host 0.0.0.0 --port 8000
curl http://localhost:8000/health
```

启动后至少验证 `/health`、`POST /chat`、`POST /chat/stream` 和 `GET /tasks/{task_id}`。生产部署推荐 Docker，并在 Nginx/网关层开启 HTTPS、SSE 长连接和足够的代理超时时间。

## 🔄 完整业务流程

本项目是“花卉需求到交易申请”的业务智能体，不是只回答问题的聊天机器人。完整流程如下：

```text
用户输入需求
  ↓
需求理解：收花人 / 关系 / 场景 / 预算 / 风格 / 色系 / 位置
  ↓
读取知识库与平台数据：花材、搭配、方案、店铺、库存、价格
  ↓
方案决策：现成方案或 DIY 定制
  ↓
返回中文说明 + 方案卡片（plan_card）
  ↓
用户修改或确认方案
  ↓
可选：生成与方案一致的预览图（image_task），平台轮询任务
  ↓
推荐可配送且具备花材/库存的店铺（shop_card）
  ↓
用户确认店铺和方案
  ↓
创建订单（order_card）
  ↓
返回支付申请/跳转参数（pay_jump）
  ↓
平台打开支付页面并处理支付回调
```

智能体负责理解、检索、设计和编排；宿主平台负责渲染页面、接收用户按钮操作、执行真实订单/支付、处理登录和回调。智能体不会直接调用平台支付 SDK，也不应绕过用户确认创建真实支付。

### 关键业务行为

1. 用户只问花卉知识时，应优先回答知识问题，不强行推荐商品。
2. 用户提出购买需求时，应结合需求和真实平台数据推荐，不凭空编造库存、价格或店铺。
3. DIY 方案必须使用知识库中的真实花材、搭配和预算规则，并返回可执行的花材数量、步骤、养护与 `effect_prompt`。
4. 方案卡片、文本回复和订单金额必须保持一致。
5. 预览图是异步任务，平台不能把任务 ID 当作图片 URL。
6. 下单前必须有明确确认；订单写入必须可追踪、可幂等，支付结果以平台回调为准。

## 🔌 平台接入契约

> ⚠️ **前端渲染由宿主平台负责，本包不携带前端。** 智能体后端只产出结构化 `ui / data / action`；接入平台**必须先实现对应的前端组件**（`plan_card` / `shop_card` / `order_card` / `pay_jump` / `image_task` / `dialog_options` / `text`），否则会出现「有数据无展示」。每个 UI 类型的数据结构、渲染要求与示例请阅读 **[FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)**，或调用 `GET /ui-contract` 拉取机器可读清单对照。

`POST /chat` 的响应保留兼容字段 `reply`、`ui`、`data`，并增加平台中立的 `action`：

```json
{
  "reply": "我为你推荐了一个生日花束方案",
  "ui": "plan_card",
  "data": {"plan_id": "P001", "name": "生日玫瑰花束", "price": 199},
  "action": {
    "type": "show_plan",
    "payload": {"ui": "plan_card", "data": {"plan_id": "P001"}, "stage": "plan_confirm"},
    "required_capabilities": ["show_plan_page"],
    "fallback": "当前平台暂不支持方案页面，请先展示文本和方案数据。"
  },
  "session_id": "会话 ID",
  "stage": "plan_confirm"
}
```

平台建议实现以下能力：

| 能力 | 平台侧职责 |
|---|---|
| `show_plan_page` | 展示方案名称、花材、价格、图片和确认/修改按钮 |
| `show_shop_page` | 展示店铺、距离、评分、配送和选择按钮 |
| `show_options` | 展示模式选择、确认和修改选项 |
| `start_image_task` | 展示生成中状态，按任务 ID 轮询图片 |
| `create_order` | 在用户确认后调用订单服务并展示订单详情 |
| `open_payment` | 使用平台支付能力打开支付页面 |

不支持某个 action 时，平台应使用 `fallback` 或 `reply` 继续对话，不要直接报错中断。`required_capabilities` 只表示接入方需要具备的能力，不是智能体可以自行调用的前端接口。

## 前端对接契约 (Frontend Contract)

**接入前必读：[FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)** —— 该文档把每个 UI 类型（`text` / `dialog_options` / `plan_card` / `shop_card` / `order_card` / `pay_jump` / `image_task`）的数据结构、渲染要求、示例 payload 和「接入前 Check 清单」写清楚。接入的开发者或 AI 在开始对接时读一遍，就能知道自己要补哪些前端组件，避免「有数据无展示」。

后端同时提供 `GET /ui-contract`，返回与文档一致的机器可读清单，方便前端/AI 在接入脚本里自动拉取、逐项对照。

关键提醒（三点）：

1. 本包是纯后端 API，**前端由宿主平台自研**；
2. 若宿主前端缺 `plan_card` / `shop_card` / `pay_jump` / `image_task` 等组件，接上后体验是坏的 —— 请先补齐再联调；
3. 暂缺的组件先用文本降级（渲染 `reply` + `action.fallback`），不要报错中断对话。

## 🧭 Vibe Coding 快速理解

如果你第一次接触本项目，可以按下面的方式理解：

- `agent/agent.py`：Agent 运行器，负责会话、ReAct 工具循环、回复和结果编排
- `agent/toolkit.py`：工具注册表和统一执行入口
- `agent/tools.py`：花艺核心能力、需求抽取、知识与方案设计
- `agent/data_tools.py`：文件、数据库发现、只读查询、自动映射和平台数据适配
- `agent/diy_tools.py`：DIY 方案、改版和生图任务的工具包装
- `agent/shop_tools.py`：店铺推荐与花材匹配
- `agent/skills/skill_order.py`：订单组装、金额核算和支付跳转参数
- `agent/engine/ui_protocol.py`：`UIType`、`ChatResponse`、`AgentAction` 等对外协议
- `domain/requirements.py`：跨模块共享的 `FlowerRequirement`
- `backend/storage/`：会话、订单、任务和数据库存储
- `backend/routers/chat.py`：平台调用的 HTTP/SSE 接口
- `agent/knowledge/*.json`：花材、风格、场景、搭配、预算和包装知识

修改项目时遵守三条原则：

1. 新能力优先实现为独立工具，通过 `@register_tool` 注册，不把业务逻辑继续堆进 `agent.py`。
2. 平台差异放在 repository/data gateway/平台适配层，智能体只依赖标准字段和 action 协议。
3. 任何会改变订单、支付或用户数据的操作都必须有权限、确认、幂等和失败回滚策略。

推荐 Vibe Coding 指令：

```text
请先阅读 README、DEPLOY、FRONTEND_CONTRACT、agent/engine/ui_protocol.py、domain/requirements.py、agent/toolkit.py，
理解“需求理解 → 数据检索 → 方案卡片 → 预览图 → 店铺 → 订单 → 支付申请”的完整链路。
修改时保持 ChatResponse 的兼容字段和 AgentAction 协议不变；先检查现有工具注册，避免重复注册；
不要让 LLM 直接执行支付，真实支付由宿主平台和后端业务服务承接。
```


### 知识库结构

```
agent/knowledge/
├── flowers.json        # 花材信息（名称、花语、养护、季节等）
├── styles.json         # 花艺风格（现代、自然、复古等）
├── scenes.json         # 应用场景（婚礼、生日、节日等）
├── pairings.json       # 搭配建议
├── budget.json         # 预算方案
├── packaging.json      # 包装建议
├── sources/            # 扩展知识源
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
  "care": {
    "light": "充足散射光",
    "water": "见干见湿",
    "temperature": "15-25°C"
  },
  "season": ["四季"],
  "colors": ["红", "粉", "白", "黄", "紫"],
  "pairing_notes": "适合搭配满天星、尤加利、洋桔梗",
  "price_range": {
    "low": 2,
    "high": 15,
    "unit": "支"
  }
}
```

### 添加新知识

1. 编辑 `agent/knowledge/` 下对应的 JSON 文件
2. 按照上述格式添加新条目
3. 重启服务生效

### 导入外部知识

使用导入脚本批量导入：

```bash
python scripts/import_flower_knowledge.py
```

支持的数据源：
- `PlantFlowerDatasets`
- `flower-db`
- `Flower-Knowledge-Graph-Visualization`
- `flora-atlas`

## 🚢 部署指南

### Docker 生产部署

```bash
# 1. 准备配置
cp .env.example .env
vim .env  # 填入生产配置（LLM、JWT_SECRET、PLATFORM_API_KEYS、POSTGRES_PASSWORD 等）

# 2. 构建并启动（自带 PostgreSQL）
docker-compose up -d --build

# 3. 可选：启用 Nginx 反向代理（HTTPS + SSE）
#    证书放 deploy/certs/，修改 deploy/nginx.conf 中的域名
docker-compose --profile nginx up -d

# 4. 设置开机自启
docker update --restart unless-stopped flora-agent

# 5. 查看状态
docker-compose ps
docker-compose logs -f agent
```

`docker-compose.yml` 内置三个服务：`postgres`（数据持久化到 `pgdata` 卷）、`agent`（等数据库健康后启动）、`nginx`（可选 profile，反向代理并托管 `/generated` 静态图）。数据库端口默认不对外暴露，需要外部访问时再取消 `postgres.ports` 的注释。

### Nginx 反向代理配置

```nginx
server {
    listen 80;
    server_name api.your-domain.com;

    # 重定向到 HTTPS
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name api.your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # API 代理
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # SSE 支持
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

### systemd 服务（裸机部署）

```bash
# 创建服务文件
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

# 启动服务
sudo systemctl daemon-reload
sudo systemctl enable flora-agent
sudo systemctl start flora-agent

# 查看状态
sudo systemctl status flora-agent
```

## ❓ 常见问题

### Q: LLM 调用报错 401？

**A**: 检查 `LLM_API_KEY` 是否正确，是否过期。确保 API Key 有足够的额度。

### Q: 文生图不生成？

**A**: 检查以下配置：
- `IMAGE_API_KEY` 是否配置
- `IMAGE_PROVIDER` 是否正确
- 不配置则跳过生图功能，使用 mock 模式

### Q: 微信登录失败？

**A**: 检查：
- `WECHAT_APPID` 和 `WECHAT_SECRET` 是否与小程序后台一致（旧名称 `WX_APPID` / `WX_SECRET` 也兼容）
- 小程序是否已发布或开启了开发版体验

### Q: 数据库连接失败？

**A**:
- 确认 `DATABASE_URL` 使用 `postgresql://`，并检查地址、端口、账号、密码和网络白名单。
- PostgreSQL 驱动为 `psycopg[binary]`，已写入 `requirements.txt`。
- 本服务不支持使用 SQLite 作为数据库。

### Q: 小程序请求被 CORS 拦截？

**A**: 检查 `ALLOWED_ORIGINS` 是否包含小程序域名（不含路径），多个域名用逗号分隔。

### Q: 工具调用超时？

**A**: 调大 `LLM_REQUEST_TIMEOUT`（默认 120 秒）和 `REQUEST_TIMEOUT`（默认 180 秒）。

### Q: 如何添加新工具？

**A**: 在 `agent/tools.py` 中添加工具定义和执行函数，重启服务即可。

### Q: 如何查看智能体思考过程？

**A**: 使用 `/chat/stream` 接口，监听 `thinking` 事件。

## 📄 许可证

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！
