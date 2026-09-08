# 花艺智能体 · 项目主文档

> **ReAct 架构的纯后端 AI 智能体服务**：把花艺对话、DIY 方案设计、AI 生图、电子贺卡、店铺/商品只读查询等能力封装为平台无关的 HTTP API，供微信小程序 / H5 / App 以最小改造成本接入。
>
> **运行时对外自称**：「你的专属花艺小助手」（仓库内的历史项目代号仅作内部资产标识，**非对外品牌名**）。

---

## 📌 项目快照

| 项 | 值 |
|---|---|
| 项目代号（内部） | `flora_agent` / 域名 `tiaowulan` / Docker 容器前缀 `flora-` |
| 当前版本 | v1.x（持续迭代中） |
| 最近更新 | 2026-09-07 |
| 代码基线 | 本地 HEAD `7b8376d`；服务器 `4d24f66`（含 09-07 修复，scp 直传生效） |
| 生产环境 | 阿里云 ECS · 广州 · 公网域名 `https://api.tiaowulan.com` |
| 部署形态 | Docker Compose 4 服务（`postgres` / `agent` / `nginx` / `dashboard`） |
| 运行时长 | 4 容器 healthy，监控面板活跃 |
| 监控面板 | https://api.tiaowulan.com/dashboard/ |
| 文档组织 | **本 README 为全仓库唯一入口**；专题文档 13 篇在 `docs/`，索引见 [§📚 完整文档索引](#-完整文档索引) |

---

## 🎯 这是什么、不是什么

**是什么**：

- 一套**纯后端** AI 智能体服务，对外只暴露 HTTP API（含 `/docs` Swagger UI）。
- **花艺垂直领域**的对话、推荐、DIY 设计、AI 生图、AI 贺卡、店铺/商品查询能力。
- **平台无关**的接入层——一套 API 服务小程序、H5、App、第三方系统（用 X-API-Key + JWT 隔离）。
- **生产级**的可观测体系：调用面板 + 健康检查 + 关键日志 + 故障排查手册。

**不是什么**：

- ❌ 不是小程序前端项目（小程序是接入方，不是本仓库产物）。
- ❌ 不是商品/订单业务后台（智能体只查平台库，不存不落订单）。
- ❌ 不是直接下单调起支付的链路（订单走小程序结算页，智能体只输出结构化推荐）。
- ❌ 不是开源 demo（合同授权项目，详见文末「版权与许可」）。

---

## ✨ 核心能力

| 能力 | 一句话描述 | 关键路径 |
|---|---|---|
| 🗣️ **对话推荐** | 基于场景（送女友 / 送长辈 / 慰问 / 开业）的花礼推荐 | `POST /chat` |
| 🌸 **DIY 方案设计** | 花材清单 → 完整方案（含配花/叶材/包装），支持「单一花材严格语义」 | 工具 `parse_diy` + `render_diy_card` |
| 🎨 **AI 生图** | 文字描述生成效果图（768×1024 等比例），OSS 转存防失效 | 工具 `generate_image_task` + `POST /images/{task_id}` |
| 💌 **AI 电子贺卡** | 5 套模板（暖色 / 淡粉 / 绿色 / 信纸 / 夜色），可定制文案与署名 | 工具 `suggest_greetings` + `render_greeting_card` |
| 🏪 **店铺只读查询** | 营业时间、配送时间、起送价、地址、电话、营业状态 | 工具 `platform_db_query_entity(entity='shop')` |
| 🌹 **商品/方案查询** | 按花材 / 场景 / 价格筛选，返回图片、SKU、价格 | 工具 `search_products` |
| 🧠 **历史会话多轮** | 按 `session_id` 回传上下文 | `GET /conversations/{sid}/messages` |
| 🔍 **跨会话历史检索** | 按关键词回溯该用户**所有**历史会话，回答「上次那家店 / 我之前买过什么」 | 工具 `search_history` |
| 💾 **偏好自动沉淀** | 对话后自动提炼用户明确表达的偏好（送花对象 / 场合 / 预算 / 色系 / 忌讳），下次开聊即带上 | 后台任务 `maybe_consolidate` |
| ⚡ **流式输出** | `tool_call` / `text` / `card` / `done` SSE 事件，工具进度可见 | `POST /chat/stream` |
| 🔐 **多平台隔离** | 一套后端服务多平台，各平台独立密钥、独立用户派生 | `POST /auth/token` + JWT |
| 📊 **调用监控** | 24h 调用量、平均延迟、按平台/工具分布、实时调用流 | 监控面板 + `/api/metrics/*` |

工具总数 ~20 个，运行时以 `agent.TOOL_REGISTRY` 为准。

---

## 🏗️ 系统形态

```
                ┌──────────────────── 平台接入方（任选多端） ────────────────────┐
                │                                                                 │
                │  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐    │
                │  │ 微信小程序  │  │   H5     │  │   App    │  │  第三方系统  │    │
                │  └─────┬──────┘  └────┬─────┘  └────┬─────┘  └──────┬───────┘    │
                └────────┼─────────────┼─────────────┼────────────────┼───────────┘
                         │ HTTPS · X-API-Key（各平台独立）               │
                         └───────────────┬─────────────────────────────────┘
                                         ▼
        ┌────────────────────────────────────────────────────────────────────────┐
        │             nginx（api.tiaowulan.com）                                 │
        │             - TLS 终止（Let's Encrypt）                                │
        │             - /dashboard/  →  dashboard 容器（纯静态）                │
        │             - /            →  agent 容器（FastAPI）                   │
        └─────────────┬─────────────────────────────────────┬──────────────────┘
                      │                                     │
                      ▼                                     ▼
   ┌─────────────────────────────┐         ┌─────────────────────────────────┐
   │   agent (FastAPI :8000)     │         │   dashboard (nginx:alpine)      │
   │   - /auth/token             │         │   /  → index.html               │
   │   - /chat, /chat/stream     │         │   /  → app.js / style.css       │
   │   - /conversations/*        │         │   ↑ 同源调 /api/metrics/*      │
   │   - /upload, /images/{tid}  │         └────────────┬────────────────────┘
   │   - /ui-contract            │                      │
   │   - /api/metrics/*          │         (DASHBOARD_API_KEY 守卫)
   │      summary/calls/         │
   │      platforms/tools/stream │
   └──────────┬──────────────────┘                      │
              │                                          │
              │                                          ▼
              ▼                                监控面板展示：
   ┌─────────────────────────┐                  - 24h 调用量、平均延迟
   │   PostgreSQL            │                  - 平台分布、工具分布
   │   - 业务表              │                  - 实时调用流（SSE）
   │   - 映射表              │                  - 错误率与时间趋势
   │   - call_logs           │
   │   - tool_call_logs      │
   └─────┬───────────────────┘
         │
         ▼ （外网受控）
   ┌─────────────────────────┐         ┌─────────────────────────────────┐
   │  平台数据库（只读）     │ ←─→     │   LLM (DashScope 阿里云百炼)    │
   │  MySQL / PG 等          │         │   - qwen3.8-flash  对话          │
   │  - plans（方案/商品）   │         │   - qwen-image-3.0  生图          │
   │  - shops（店铺）        │         │   (hy fallback 默认关闭)         │
   │  - orders（订单）       │         └─────────────────────────────────┘
   └─────────────────────────┘
```

---

## 🧱 技术栈

| 层 | 选型 | 备注 |
|---|---|---|
| LLM（对话） | 阿里云百炼 DashScope `qwen3.8-flash` | 默认关闭思考（`LLM_ENABLE_THINKING=False`），端到端 ~3s |
| LLM（生图） | 阿里云百炼 `qwen-image-3.0` | 走原生 `multimodal-generation`，OSS 转存防失效 |
| LLM SDK | `openai` Python SDK（兼容模式） | 同一套接口切 Qwen / hy |
| Agent 框架 | 自研 ReAct（`agent/engine`） | 工具注册表 + 多轮记忆 + 流式回调 |
| 后端框架 | FastAPI + Pydantic v2 | 异步路由 + 自动 OpenAPI 文档 |
| ORM / DB | SQLAlchemy + psycopg + asyncpg | PostgreSQL，外部库按方言兼容（MySQL/PG） |
| 缓存 | Redis | 限流 + 会话计数 |
| 任务 | 内存 + asyncio | 生图走后台任务，结果落数据库 |
| 部署 | Docker + Compose | 4 服务编排；nginx 配置 bind-mount，可热 reload |
| 反代 / TLS | nginx + Let's Encrypt | `proxy_buffering off` 配合 SSE |
| 鉴权 | JWT (HS256) + X-API-Key | `JWT_SECRET ≥ 32` 字符（生产强制） |
| 可观测 | 自研 `backend/observability.py` | best-effort，异常绝不阻断主链路 |
| 仓库工程 | 文档化代码 + Ruff + 标准分层 | 见 [`docs/03-系统架构设计`](./docs/03-系统架构设计.md) |

---

## 📁 仓库结构

```
flora_agent_package/
├── README.md                  ← 本文档
├── main.py                    ← 入口
├── agent/                     ← 智能体核心
│   ├── engine/                ←   ReAct 调度 + LLM 调用
│   ├── tools/                 ←   工具实现（~20 个：DIY、生图、贺卡、查询等）
│   ├── skills/                ←   贺卡 PIL 模板
│   ├── knowledge/             ←   花材库（花语、季节、搭配）
│   └── __init__.py            ←   触发 @register_tool 导入注册
├── backend/                   ← API 与业务后端
│   ├── routers/               ←   auth / chat / upload / metrics ...
│   ├── data_gateway/          ←   平台只读查询（external.py，强制 READ ONLY）
│   ├── storage/               ←   图像存储 + 任务调度
│   ├── observability.py       ←   调用埋点（调用/工具/LLM 三层）
│   ├── config.py              ←   pydantic-settings 配置
│   └── db.py                  ←   数据库连接 + init_db 自动建表
├── docs/                      ← 项目文档集（13 篇，按编号导航）
├── deploy/                    ← 部署脚本 + 监控脚本 + 证书
├── migrations/                ← 数据库迁移 SQL
├── data/                      ← 运行时生成（图像、会话文件）
├── docker-compose.yml         ← 4 服务编排
├── Dockerfile                 ← agent 镜像构建
├── .env.example               ← 配置样例（密钥按层隔离）
└── requirements.txt
```

---

## 🚀 快速开始（开发环境）

```bash
# ① 克隆与准备
git clone <repo-url> flora_agent_package
cd flora_agent_package

# ② Python 依赖（建议用 managed venv）
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# ③ 准备 PostgreSQL/Redis（用 docker 也可）
# 推荐：docker compose up -d postgres redis

# ④ 配置：从样例复制，按需改
cp .env.example .env
# 必改项：
#   JWT_SECRET            生产 ≥32 字符随机串
#   POSTGRES_*            本地默认即可
#   LLM_API_KEY           阿里云百炼 DashScope
#   IMAGE_API_KEY         同上
# 多平台接入：
#   PLATFORM_API_KEYS=<platform_id>=<key>[,<platform_id>=<key>]
# 平台只读库（如已对接平台方）：
#   PLATFORM_DB_<ID>_URL=postgresql://readonly:...  ← 强制 READ ONLY

# ⑤ 初始化数据库（IF NOT EXISTS 幂等创建业务表 + 监控表）
python -c "from backend.db import init_db; init_db()"

# ⑥ 启 agent（开发态带 reload）
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# ⑦ 看 API 文档
open http://localhost:8000/docs      # Swagger UI
open http://localhost:8000/ui-contract  # UI 卡片契约样例
```

30 秒冒烟：

```bash
# 健康检查
curl -s http://localhost:8000/health

# 拿 token（用 .env 里配置的 PLATFORM_API_KEYS 第一对）
curl -sX POST http://localhost:8000/auth/token \
  -H 'X-API-Key: <your-key>' \
  -d 'external_user_id=demo-user-1'
# → {"access_token":"...","user_id":"<hash>"}

# 发起对话
curl -sX POST http://localhost:8000/chat \
  -H "Authorization: Bearer <access_token>" \
  -H 'Content-Type: application/json' \
  -d '{"message":"送女友什么花好？","user_id":"<hash>"}'
```

更详细的接入示例见 [`docs/04-API接口文档`](./docs/04-API接口文档.md) + [`docs/05-前端对接契约`](./docs/05-前端对接契约.md)。

---

## 🌐 部署（生产）

详见 [`docs/07-部署手册`](./docs/07-部署手册.md)。一段话版本：

```bash
# 服务器标准上线（先本地 commit + 推 GitHub 再来执行）
ssh <REDACTED_HOST>
cd /opt/flora_agent_package
git fetch && git merge --ff-only origin/main
docker compose --profile nginx up -d --build agent dashboard nginx
docker exec flora-nginx nginx -t && docker exec flora-nginx nginx -s reload
```

> **关键约定**：
> - 改代码必须 `docker compose up -d --build agent`（代码 bake 进镜像，restart 无效）。
> - 改 nginx 配置只需 `nginx -s reload`（配置 bind-mount）。
> - 改监控面板前端只需 `docker compose up -d --build dashboard`（连带 agent 重启，等 ~30s）。
> - 沙箱推 GitHub 可能波动（`Connection was reset`），兜底是 `scp` 直传 + 服务器重建。

---

## 🛠️ 工具注册速查（按类别）

| 类别 | 工具 | 简述 |
|---|---|---|
| 对话基础 | `retrieve_knowledge` | 检索花卉知识库 |
| 对话基础 | `query_history` | 查历史会话 |
| 商品 | `search_products` | 按花材 / 场景 / 价格筛选方案 |
| DIY | `parse_diy` | 解析花材清单 + 预算 + 风格 |
| DIY | `render_diy_card` | 渲染结构化 DIY 卡片 |
| AI 生图 | `generate_image_task` | 文字 → 图片（Qwen-image-3.0） |
| 贺卡 | `suggest_greetings` | 推荐贺卡文案 |
| 贺卡 | `render_greeting_card` | 5 模板渲染（含中文字体探测） |
| 平台只读 | `platform_db_query_entity` | entity=plan/shop/order/user 实时查平台库 |
| 记忆 | `search_history` | 跨会话检索该用户历史对话（按 `user_id` 隔离，只读） |
| 记忆 | `save_memory` / `save_user_profile` | 写入长期偏好到 `user_preferences` |
| 上下文注入 | `inject_context` | 店铺锁定 / 平台映射上下文注入 |

完整定义见 `agent/tools/`，运行时清单 = `len(agent.TOOL_REGISTRY)`。

---

## 📡 API 速查（端点清单）

| 端点 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/health` | GET | 无 | 健康检查（容器探活） |
| `/auth/token` | POST | X-API-Key | X-API-Key + external_user_id → JWT |
| `/chat` | POST | JWT | 非流式对话 |
| `/chat/stream` | POST | JWT | SSE 流式对话 |
| `/chat/reset` | POST | JWT | 清会话上下文 |
| `/conversations` | GET | JWT | 列出会话 |
| `/conversations` | POST | JWT | 新建会话 |
| `/conversations/{sid}/messages` | GET | JWT | 列消息 |
| `/upload` | POST | JWT | 上传图片 |
| `/images/{task_id}` | GET | JWT | 查生图任务状态与结果 |
| `/ui-contract` | GET | 无 | UI 卡片契约样例（8 种） |
| `/api/metrics/summary` | GET | DASHBOARD_API_KEY | 24h 调用量/平均延迟/错误率 |
| `/api/metrics/calls` | GET | DASHBOARD_API_KEY | 调用明细 |
| `/api/metrics/platforms` | GET | DASHBOARD_API_KEY | 按平台分布 |
| `/api/metrics/tools` | GET | DASHBOARD_API_KEY | 按工具分布 |
| `/api/metrics/stream` | GET | DASHBOARD_API_KEY | SSE 实时调用流 |

请求/响应 Schema、错误码、字段全部在 [`docs/04-API接口文档`](./docs/04-API接口文档.md)。

---

## 📊 监控 & 运维

- **面板入口**：https://api.tiaowulan.com/dashboard/（密钥见 `.env` 的 `DASHBOARD_API_KEY`，登录后只存 sessionStorage）
- **指标**：24h 调用量、平均延迟、错误率、按平台 / 工具分布、实时调用流
- **关键命令**：
  ```bash
  docker ps                                    # 看 4 容器 healthy
  curl -s http://api.tiaowulan.com/health     # 探活
  docker logs --since 10m flora-agent | tail -100
  docker exec -e PYTHONPATH=/app flora-agent python -c '...'
  ```
- **故障排查手册**：[`docs/08-运维监控手册 §5`](./docs/08-运维监控手册.md)
- **已知问题 K/T 系列 + 历史修复**：[`docs/08 §7`](./docs/08-运维监控手册.md) · [`docs/11-变更记录`](./docs/11-变更记录.md)

---

## 🧭 关键架构决策（**勿回退**）

| # | 决策 | 一句话说明 |
|---|---|---|
| 1 | **平台只读架构** | 商品 / 店铺 / 订单实时只读查平台库，本地不存不镜像不落 |
| 2 | **智能体不直接下单** | 输出结构化 `reply + products`，由用户在小程序现有结算页确认 |
| 3 | **AI 贺卡模板化** | 5 套模板（PIL 合成 900×1200），中文字体自动探测 |
| 4 | **店铺锁定模式** | `/chat` 带 `shop_id` 时四层贯通（SQL / 工具 / DIY / 下单走前端） |
| 5 | **LLM 单一主 provider** | 默认只剩阿里云百炼 Qwen；hy fallback 默认关闭（`LLM_HY_FALLBACK_ENABLED=False`） |

每条决策的背景与论证见 [`docs/03-系统架构设计`](./docs/03-系统架构设计.md) + [`docs/11-变更记录`](./docs/11-变更记录.md)。

---

## 🔐 安全与合规要点

| 要点 | 落地 |
|---|---|
| 多平台隔离 | 每个接入方持独立 `X-API-Key`；`user_id = hash(platform_id, external_user_id)`，永不撞库 |
| JWT | HS256；`JWT_SECRET ≥ 32` 字符（生产强制） |
| CORS | 生产默认 `*`，按接入方收敛 |
| 匿名登录 | 生产 `ANONYMOUS_LOGIN_ENABLED=False` |
| 平台库只读 | `data_gateway/external.py` 强制 `SET TRANSACTION READ ONLY`，禁止回写 |
| 异常回显 | LLM 调用失败对用户隐藏原始异常文案（仅 logger 落盘，K-1 修复） |
| 密钥分层 | LLM / DB / 平台 / API key 各自从 `.env` 注入；`.env` 已 `.gitignore`，永不进仓 |
| 监控鉴权 | `/api/metrics/*` 三种 key 携带方式：`X-Dashboard-Key` / `Authorization: Bearer` / `?key=`（K-2 已修） |

完整信任模型与合规边界见 [`docs/10-安全与合规`](./docs/10-安全与合规.md)。

---

## 📚 完整文档索引

> **本节即为全仓库文档总索引**（含文档集地图、按角色推荐阅读路径与维护约定）。
>
> 专题文档 13 篇位于 [`docs/`](./docs/)；维护规范、版本线、与历史归档文档对应关系均在此节。

| 编号 | 文档 | 主要读者 |
|---|---|---|
| 01 | [项目概述](./docs/01-项目概述.md) | 所有人 |
| 02 | [需求规格 (PRD)](./docs/02-需求规格(PRD).md) | 产品 / 研发 / 测试 |
| 03 | [系统架构设计](./docs/03-系统架构设计.md) | 研发 |
| 04 | [API 接口文档](./docs/04-API接口文档.md) | 研发 / 接入方 |
| 05 | [前端对接契约](./docs/05-前端对接契约.md) | 小程序 / 前端 |
| 06 | [数据库设计](./docs/06-数据库设计.md) | 研发 / DBA |
| 07 | [部署手册](./docs/07-部署手册.md) | 运维 / SRE |
| 08 | [运维监控手册](./docs/08-运维监控手册.md) | 运维 / SRE |
| 09 | [测试与验收](./docs/09-测试与验收.md) | 测试 / 研发 |
| 10 | [安全与合规](./docs/10-安全与合规.md) | 安全 / 研发 |
| 11 | [变更记录](./docs/11-变更记录.md) | 所有人 |
| — | [小程序接入待办清单](./docs/小程序接入待办清单.md) | 小程序团队 |

### 📖 按角色推荐阅读路径

| 角色 | 路径（先看本 README，再按序展开） |
|---|---|
| 🧭 新人交接 / 研发入门 | [01 项目概述](./docs/01-项目概述.md) → [03 系统架构设计](./docs/03-系统架构设计.md) → [04 API 接口文档](./docs/04-API接口文档.md) → [06 数据库设计](./docs/06-数据库设计.md) → [10 安全与合规](./docs/10-安全与合规.md) |
| 📱 小程序 / H5 / 前端接入 | [05 前端对接契约](./docs/05-前端对接契约.md) → [04 API 接口文档](./docs/04-API接口文档.md) → [小程序接入待办清单](./docs/小程序接入待办清单.md) |
| 🚀 部署 / 运维 / SRE | [07 部署手册](./docs/07-部署手册.md) → [08 运维监控手册](./docs/08-运维监控手册.md) → [11 变更记录](./docs/11-变更记录.md) → [10 安全与合规 §2–3](./docs/10-安全与合规.md) |
| 🛡️ 安全 / 合规审计 | [10 安全与合规](./docs/10-安全与合规.md) → [03 系统架构设计 §6 ADR](./docs/03-系统架构设计.md) → [11 变更记录](./docs/11-变更记录.md) |
| 🔍 故障排查（oncall 直跳） | [08 运维监控手册 §5 故障排查手册](./docs/08-运维监控手册.md) + [§7 已知问题 K/T 系列](./docs/08-运维监控手册.md) |
| 🧪 测试 / 验收 | [02 需求规格 §6 验收标准](./docs/02-需求规格(PRD).md) → [09 测试与验收](./docs/09-测试与验收.md) |
| 📦 产品 / 需求管理 | [01 项目概述](./docs/01-项目概述.md) → [02 需求规格 (PRD)](./docs/02-需求规格(PRD).md) → [11 变更记录](./docs/11-变更记录.md) |

### 🗃️ 与归档旧文档的关系

历史散落的 README / DEPLOY / FRONTEND_CONTRACT 等文档（部分仍写「LLM = 腾讯 hy」「生图仅 mock/hy」等，已过时）已统一收口到 [`docs/archive/`](./docs/archive/)，**仅供追溯**。请一律以本文档集为准：

- `docs/archive/README.md` → 已被「本仓库根 README + 01 + 03」取代
- `docs/archive/DEPLOY.md` / `docs/archive/广州ECS部署清单.md` → 已被 [07 部署手册](./docs/07-部署手册.md) 取代
- `docs/archive/FRONTEND_CONTRACT.md` → 已被 [05 前端对接契约](./docs/05-前端对接契约.md) 取代
- `docs/archive/接入说明-花艺智能体API.md` / `docs/archive/服务接入8问答.md` → 已被 [04 API 接口文档](./docs/04-API接口文档.md) 取代

> ⚠️ **生效中的关键反转（2026-09-07）**：LLM 已切为**阿里云百炼 Qwen**（`qwen3.8-flash` 对话 / `qwen-image-3.0` 生图），hy 不再作为默认 provider（`LLM_HY_FALLBACK_ENABLED=False`）。任何仍写「腾讯 hy」的旧内容均为过时信息。

### 📐 文档维护约定

- **每篇头部**都需有「文档信息」表（版本 / 日期 / 适用 commit / 维护人 / 读者 / 权威来源）。
- **结论取自代码**：架构、接口、配置以仓库源码为权威；引用格式 `文件:行号` 或 `git HEAD <commit>`。
- **可运行数字**：工具数量、字段映射等以**运行时实际状态**为准（如「工具总数 ≈ 20，运行时以 `agent.TOOL_REGISTRY` 为准」）。
- **变更同步**：每次架构或接口级变更，必须在 [11 变更记录](./docs/11-变更记录.md) 增条目，并更新受影响文档顶部的「适用 commit」。
- **命名约定**：历史项目代号（仓库内旧称）只用于内部资产标识（容器名、域名、日志前缀），**不是对外品牌名**。对外一律称「花艺智能体」或运行时口语化表达「你的专属花艺小助手」。

### 🧾 文档集版本线

| 日期 | 文档集版本 | 代码基线 | 主要变化 |
|---|---|---|---|
| 2026-09-07 | **v2.1（本文档唯一化）** | 本地 `7b8376d` / 服务器 `4d24f66`（scp 直传生效） | `docs/README.md` 废除，全仓库文档入口并入根 `README.md` 的「📚 完整文档索引」段 |
| 2026-09-07 | v2.0 | `7b8376d` / 服务器 `4d24f66` | 新建仓库根 `README.md`（项目快照）；K-1~K-3、T-1~T-4 全部修复 |
| 2026-09-07 | v1.0 | `1593e76` | 首次按标准模板（13 篇）建立文档集，旧文档归档 `docs/archive/` |
| 2026-09-04 及之前 | 散落 README / DEPLOY / FRONTEND_CONTRACT | `aa91d1c` 之前 | 文档分散、内容重叠、部分过时（已归档） |

---

## 🗺️ 路线图

**已完成（最近一次更新 2026-09-07）**
- ✅ 调用监控面板上线（commit `87e98d0`）
- ✅ LLM 切阿里云百炼 Qwen（`1a72ab6`）
- ✅ 店铺映射补字段 / 解析敏感字段（K-3，`48a84fb` + 数据补丁）
- ✅ 异常回显安全兜底（K-1，`48a84fb`）
- ✅ 监控 SSE `?key=` 校验（K-2，`48a84fb`）
- ✅ DIY 单花解析 T-1（含 `红玫瑰11朵` 类明文）
- ✅ 关闭 hy 跨厂商兜底 T-2（`LLM_HY_FALLBACK_ENABLED=False`）
- ✅ Qwen 关思考提速 T-3（端到端 ~3s，提速 5 倍）
- ✅ discover_external MySQL 双坑修复 T-4（行键 + FK 方言）

**进行中**
- 🚧 小程序对接：流式输出改造 / DIY 卡片 / 历史会话 / 店铺查询（[`docs/小程序接入待办清单`](./docs/小程序接入待办清单.md)）

**待平台方提供**
- 各平台只读连接串 `PLATFORM_DB_<ID>_URL`
- 激活 mapping（plan / shop / order / user）

**待评估**
- 真流式 LLM 调用（`call_llm_stream`）
- 多语言 / 国际化
- 企业微信 / 公众号接入

---

## 📮 反馈与维护

- **仓库 owner / 维护人**：见 [`docs/01-项目概述 §1`](./docs/01-项目概述.md)
- **报送问题**：内部 IM / GitHub Issue / 走运维故障排查手册流程
- **内部代号说明**：`flora_agent`（仓库名）/ `tiaowulan`（域名）/ Docker 容器前缀 `flora-` —— 这些词**只用于内部资产标识，不是对外品牌名**。对外一律称「花艺智能体」或运行时自称「你的专属花艺小助手」。

---

## 📝 版权与许可

仅供合作方内部使用，合同授权范围外禁止外发、二次分发或反向工程。

---

> **项目状态**：🟢 在跑 · **最后核验**：2026-09-07（HEAD `4d24f66`，scp 直传生效中）
