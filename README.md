# 花艺智能体 · 项目主文档

> **ReAct 架构的纯后端 AI 智能体服务**：把花艺对话、DIY 方案设计、AI 生图、电子贺卡、平台商品/店铺只读查询等能力封装为平台无关的 HTTP API，供微信小程序 / H5 / App / 官网以最小改造成本接入。
>
> **运行时对外自称**：「你的专属花艺小助手」（仓库内的历史项目代号仅作内部资产标识，**非对外品牌名**）。

---

## 📌 项目快照

| 项 | 值 |
|---|---|
| 项目代号（内部） | `flora_agent` / 域名 `tiaowulan` / Docker 容器前缀 `flora-` |
| 当前版本 | **1.2.0** |
| 最近更新 | **2026-09-21** |
| 代码基线 | `main` 提交 `3feadb2`；生产使用已验证发布包，服务器目录不是 git 仓库 |
| 生产环境 | **腾讯云 · 北京** · 公网域名 `https://api.tiaowulan.com`（ICP 备案已通过） |
| 部署形态 | Docker Compose **6 服务**：常驻 4（`postgres` / `agent` / `nginx` / `dashboard`）+ 体验版 2（`postgres-demo` / `agent-demo`，profile `demo`） |
| 对外入口 | 生产 API `https://api.tiaowulan.com` · 体验演示页 `https://api.tiaowulan.com/demo/` · 官网体验窗 `https://www.tiaowulan.com/agent.html` |
| 监控面板 | `https://api.tiaowulan.com/dashboard/` |
| 自动化测试 | **759 条** pytest（`pytest -q` 全绿）+ 场景评测集 + 对话冒烟脚本 |
| 当前生产开关 | `CARE_TOOL_SCOPE_ENABLED=false`、`AGENT_INTENT_ROUTING_ENABLED=false` |
| 当前演示开关 | 两项均为 `true`，用于真实 token / 延迟 / 质量对比 |
| 文档组织 | 本 README → [`DELIVERY.md`](./DELIVERY.md) → `docs/`；历史资料见 `docs/archive/` |

---

## 🎯 这是什么、不是什么

**是什么**：

- 一套**纯后端** AI 智能体服务，对外只暴露 HTTP API（含 `/docs` Swagger UI）。
- **花艺垂直领域**的对话、推荐、DIY 设计、AI 生图、AI 贺卡、平台商品/店铺只读查询能力。
- **平台无关**的接入层——一套 API 服务小程序、H5、App、第三方系统（用 `X-API-Key` + JWT 隔离）。
- **生产级**的可观测体系：调用面板 + 健康检查 + 关键日志 + 故障排查手册。
- 一套**隔离的体验演示栈**：真 LLM + 独立数据库，给客户临时体验「方案 + 建议」，不触碰生产数据。

**不是什么**：

- ❌ 不是小程序前端项目（小程序是接入方，不是本仓库产物）。
- ❌ 不是商品/订单业务后台（智能体只读查平台数据，**不存不落订单**）。
- ❌ 不是下单调起支付的链路（已明确**不接订单**，智能体只输出结构化方案与推荐）。
- ❌ 不是开源 demo（合同授权项目，详见文末「版权与许可」）。

---

## ✨ 核心能力

| 能力 | 一句话描述 | 关键工具 / 端点 |
|---|---|---|
| 🗣️ **对话推荐** | 基于场景（送女友 / 送长辈 / 慰问 / 开业）的花礼推荐，**理解口语化表达**（「手头不宽裕」「想让她开心一下」） | `POST /chat` |
| 🌸 **DIY 方案设计** | 花材清单 → 完整方案（含配花 / 叶材 / 包装 / 支数），支持「单一花材严格语义」 | `generate_diy_plan` · `revise_diy_plan` |
| 🎨 **AI 生图** | 按方案**真实花材与支数**生成效果图（768×1024），OSS 转存防失效；异步任务 + 轮询 | `generate_effect_image` · `GET /tasks/{task_id}` |
| 💌 **AI 电子贺卡** | 5 套模板（暖色 / 淡粉 / 绿色 / 信纸 / 夜色），可定制文案与署名，水印固定「以花传情」 | `suggest_greetings` · `render_greeting_card` |
| 🏪 **平台商品只读查询** | 实时查平台在售商品（图片 / 价格 / 花材构成 / 评分 / 库存），**自动去重并渲染商品卡** | `platform_db_query_entity` |
| 🧠 **多轮会话** | 按 `session_id` 保持上下文；需求跨轮累积（送花对象 / 场合 / 预算 / 色系） | `GET /conversations/{id}/messages` |
| 🔍 **跨会话历史检索** | 按关键词回溯该用户**所有**历史会话，回答「上次那家店 / 我之前买过什么」 | `search_history` |
| 💾 **偏好自动沉淀** | 对话后自动提炼用户明确表达的偏好，下次开聊即带上 | `get_user_profile` · `save_user_profile` |
| 🔀 **可点选选项** | 用户没头绪时给出方向选项（独立工具，模型可主动调用） | `show_options` |
| ⚡ **流式输出** | `tool_call` / `text` / `card` / `done` SSE 事件，工具进度可见 | `POST /chat/stream` |
| 🔐 **多平台隔离** | 一套后端多平台，`platform_id` 为身份隔离边界，各平台独立密钥、独立用户派生 | `POST /auth/token` + JWT |
| 🧪 **隔离体验栈** | 独立容器 + 独立库 + 独立密钥；能力范围可收窄到「只做方案与建议」 | `PLATFORM_ALLOWED_ENTITIES` · `/demo/` |
| 🛡️ **防编造护栏** | 「未查证即作答」「谎称已出图」「该出卡只写文字」「内部独白泄漏」四类**确定性拦截** | `agent/agent.py` |
| 📊 **调用监控** | 24h 调用量、平均延迟、按平台/工具分布、实时调用流 | 监控面板 + `/api/metrics/*` |

**工具面**：注册 **22 个**工具，其中默认 C 端可见约 **14 个**功能工具；另 **8 个**平台接入工具打 `ops` 标签、默认隐藏。实验路由开启时，明显养护 / 贺卡 / 生图请求会进一步收窄工具集。
运行时以 `agent.toolkit.visible_tool_specs()` 为准。

---

## 🏗️ 系统形态

```
        ┌─────────────── 接入方（同一业务方可共用一把 key，跨端记忆连续）───────────────┐
        │                                                                            │
        │  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌─────────┐  │
        │  │ 微信小程序  │  │   H5     │  │   App    │  │  第三方系统  │  │ 官网体验窗│  │
        │  └─────┬──────┘  └────┬─────┘  └────┬─────┘  └──────┬───────┘  └────┬────┘  │
        └────────┼──────────────┼─────────────┼───────────────┼───────────────┼───────┘
                 │              │             │               │               │
                 │  HTTPS · X-API-Key（各业务方独立）           │        POST /api/agent/query
                 └──────────────┴──────┬──────┴───────────────┘       （单轮·纯规则引擎·IP 限流）
                                       ▼
        ┌──────────────────────────────────────────────────────────────────────────┐
        │                nginx（api.tiaowulan.com · 阿里云证书）                    │
        │   ⚠️ 上游一律用**变量式 proxy_pass**（每次重解析，容器重建换 IP 不受影响） │
        │   /            → agent:8000        /demo/     → 静态 demo/index.html      │
        │   /dashboard/  → dashboard:80      /demo-api/ → agent-demo:8010          │
        └────────┬────────────────────────────────────────────────┬────────────────┘
                 │                                                │
                 ▼                                                ▼
   ┌──────────────────────────────┐              ┌──────────────────────────────────┐
   │  agent (FastAPI :8000)       │              │  agent-demo (同镜像 :8010)        │
   │  ├ 对话   /chat /chat/stream │              │  · 独立 .env.demo / 独立 JWT      │
   │  ├ 鉴权   /auth/*            │              │  · PLATFORM_ALLOWED_ENTITIES=plan │
   │  ├ 历史   /conversations/*   │              │    → 只做方案与建议，无店铺/下单   │
   │  ├ 任务   /tasks/{id}        │              │  · 独立库 flora_demo              │
   │  ├ 契约   /ui-contract       │              └──────────────┬───────────────────┘
   │  └ 监控   /api/metrics/*     │                             │
   └───────┬──────────────┬───────┘                             ▼
           │              │                        ┌──────────────────────────┐
           ▼              ▼                        │  postgres-demo           │
   ┌───────────────┐  ┌───────────────────┐        │  （卷 pgdata_demo，隔离） │
   │ PostgreSQL    │  │ LLM 阿里云百炼     │        └──────────────────────────┘
   │ 业务表/映射表  │  │ · qwen3.8-flash   │
   │ call_logs     │  │ · qwen-image-3.0  │        ┌──────────────────────────┐
   │ tool_call_logs│  └───────────────────┘        │  平台商家后台（只读 REST） │
   └───────────────┘                               │  aistore.xiangbinmeigui   │
                                                   │  /v1/merchant/*           │
                                                   └──────────────────────────┘
```

> **数据通路两代并存**：① 直连平台只读库（`PLATFORM_DB_<ID>_URL`，**DB 优先**）；
> ② 平台只读 REST（`PLATFORM_API_<SOURCE_ID>_URL`，适配层 `backend/data_gateway/http_source.py`）。
> 当前生产使用的是 ②。

---

## 🧱 技术栈

| 层 | 选型 | 备注 |
|---|---|---|
| LLM（对话） | 阿里云百炼 DashScope `qwen3.8-flash` | `LLM_ENABLE_THINKING=False`；**单轮 38–51s**（工具调用较多，待优化） |
| LLM（生图） | 阿里云百炼 `qwen-image-3.0` | 走原生 `multimodal-generation`，OSS 转存防失效 |
| LLM SDK | `openai` Python SDK（兼容模式） | 同一套接口可切其它兼容厂商 |
| Agent 框架 | 自研 ReAct（`agent/engine`） | 工具注册表 + 多轮记忆 + 流式回调 + 轮数上限 |
| 后端框架 | FastAPI + Pydantic v2 | 异步路由 + 自动 OpenAPI 文档 |
| DB 驱动 | psycopg3（直连，无 ORM） | PostgreSQL；外部只读库按方言兼容 |
| 限流 | 进程内固定窗口（`backend/rate_limit.py`） | 按用户 N 次/分 + **IP 维度**（体验版公开入口必需）；多实例需 Redis |
| 成本护栏 | token 日预算（`agent/engine/budget.py`） | `LLM_COST_ENABLED` + Redis + 阈值>0 **三者齐备**才生效 |
| 后台任务 | 独立线程池 + 独立事件循环 | 生图任务落 `image_tasks`，前端轮询 `/tasks/{id}` |
| prompt 管理 | **外置模板** `agent/prompts/*.md` | `agent.py::_build_system` 只做条件编排 → 改文案只改 md |
| 部署 | Docker + Compose（6 服务 / 2 profile） | `nginx`+`dashboard` 走 `--profile nginx`；体验栈走 `--profile demo` |
| 反代 / TLS | nginx + 阿里云免费证书 | ⚠️ 上游必须**变量式** `proxy_pass`；改配置需 **restart**，`reload` 无效 |
| 鉴权 | JWT (HS256) + X-API-Key | `JWT_SECRET ≥ 32` 字符（生产强制） |
| 可观测 | 自研 `backend/observability.py` | best-effort，异常绝不阻断主链路 |
| 测试 | pytest（432 条）+ 冒烟脚本 | 含结构护栏 / prompt 契约 / 检索评测回归门 |
| 代码质量 | Ruff（lint + format） | 类型注解 + docstring（Args / Returns / Raises） |

---

## 📁 仓库结构

```
flora_agent_package/
├── README.md                  ← 本文档（全仓库唯一入口）
├── main.py                    ← FastAPI 入口（/health + 路由挂载）
├── agent/                     ← 智能体核心
│   ├── agent.py               ←   ReAct 主体：prompt 编排 / 护栏 / 清理链
│   ├── toolkit.py             ←   工具注册表 + 可见性过滤（ops / entity 白名单）
│   ├── tools.py               ←   DIY 设计、需求抽取、规则引擎兜底
│   ├── diy_tools.py           ←   DIY / 生图工具
│   ├── data_tools.py          ←   平台只读查询工具（含 8 个 ops 工具）
│   ├── memory_tools.py        ←   记忆 / 画像工具
│   ├── shop_materials.py      ←   锁店「该店可提供原料」推断（从商品文案提取）
│   ├── requirements.py        ←   结构化需求模型
│   ├── ports.py               ←   入口上下文规范化（锁店判定）
│   ├── prompts/               ←   外置 prompt 模板（.md）
│   ├── engine/                ←   ReAct 调度 + LLM 调用 + 预算
│   ├── knowledge/             ←   花艺知识库（8 域）
│   ├── skills/                ←   贺卡 PIL 模板
│   └── mcp_servers/
├── backend/                   ← API 与业务后端
│   ├── routers/               ←   auth / chat / metrics / learning / agent_page
│   ├── data_gateway/          ←   平台只读查询（external.py 强制 READ ONLY + http_source.py）
│   ├── storage/               ←   db / diy / memory / tasks / object_store
│   ├── auth.py                ←   X-API-Key → JWT、用户身份派生
│   ├── config.py              ←   pydantic-settings 配置
│   ├── embedding.py           ←   embedding 通道（默认关）
│   ├── rate_limit.py          ←   进程内限流
│   └── observability.py       ←   调用埋点（调用/工具/LLM 三层）
├── demo/                      ← 体验演示页（单文件零构建 index.html）
├── scripts/                   ← 运维 / 评测 / 冒烟脚本
│   ├── agent_smoke.py         ←   对话冒烟测试（按分组跑真实对话 + 质量检查）
│   ├── gen_demo_env.py        ←   从生产 .env 派生 .env.demo
│   ├── eval_retrieval.py      ←   检索评测
│   └── ...
├── tests/                     ← pytest（432 条）+ eval/ 评测集
├── docs/                      ← 项目文档集（按编号导航）
├── deploy/                    ← nginx.conf / 证书脚本 / 监控脚本 / env.demo.example
├── migrations/                ← 数据库迁移 SQL
├── data/                      ← 运行时生成（已 gitignore）
├── docker-compose.yml         ← 6 服务编排（2 profile）
├── Dockerfile                 ← agent 镜像构建
├── .env.example               ← 配置样例（密钥按层隔离）
└── requirements.txt
```

---

## 🚀 快速开始（开发环境）

```bash
# ① 准备
git clone <repo-url> flora_agent_package
cd flora_agent_package

# ② Python 依赖（建议用 managed venv）
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# ③ 起 PostgreSQL（用 docker 最省事）
docker compose up -d postgres

# ④ 配置
cp .env.example .env
# 必改项：
#   JWT_SECRET            生产 ≥32 字符随机串
#   POSTGRES_*            本地默认即可
#   LLM_API_KEY           阿里云百炼 DashScope
#   IMAGE_API_KEY         同上
# 多平台接入（platform_id = 身份隔离边界）：
#   PLATFORM_API_KEYS=<platform_id>=<key>[,<platform_id>=<key>]
# 平台只读数据源（二选一，DB 优先）：
#   PLATFORM_DB_<ID>_URL=postgresql://readonly:...      ← 强制 READ ONLY
#   PLATFORM_API_<SOURCE_ID>_URL=https://...            ← 只读 REST

# ⑤ 初始化数据库（IF NOT EXISTS 幂等建表）
python -c "from backend.storage.db import init_db; init_db()"

# ⑥ 启 agent（开发态带 reload）
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

30 秒冒烟：

```bash
curl -s http://localhost:8000/health

# 换 token（用 .env 里 PLATFORM_API_KEYS 的第一对）
curl -sX POST http://localhost:8000/auth/token \
  -H 'X-API-Key: <your-key>' -H 'Content-Type: application/json' \
  -d '{"external_user_id":"demo-user-1"}'
# → {"access_token":"...","user_id":"<hash>"}

# 对话（user_id 必须与 token 一致）
curl -sX POST http://localhost:8000/chat \
  -H "Authorization: Bearer <access_token>" -H 'Content-Type: application/json' \
  -d '{"message":"送女友生日花，预算300","user_id":"<hash>"}'
```

> ⚠️ **Windows cmd 用户注意**：JSON 不能用单引号包裹，请存成文件用 `-d @body.json`，或改用 PowerShell / Git Bash。

**对话冒烟测试**（改完 prompt / 工具 / 护栏推荐跑）：

```bash
python scripts/agent_smoke.py --list                    # 列用例（不花钱）
python scripts/agent_smoke.py --group basic             # 跑一组
python scripts/agent_smoke.py --all                     # 全量（慢，真实计费）
```

更详细的接入示例见 [`docs/04-API接口文档`](./docs/04-API接口文档.md) + [`docs/05-前端对接契约`](./docs/05-前端对接契约.md) + [`docs/接入方调用示例`](./docs/接入方调用示例.md)。

---

## 🌐 部署（生产）

详见 [`docs/07-部署手册`](./docs/07-部署手册.md)。一段话版本：

```bash
# 代码在服务器 ~/flora_agent（⚠️ 该目录不是 git 仓库，用 scp 直传）
scp -i ~/.ssh/flora_server.pem <本地文件> ubuntu@<服务器>:/home/ubuntu/flora_agent/<路径>

# 上传后必须转换行（本地是 CRLF）
ssh ... 'cd ~/flora_agent && sed -i "s/\r$//" <改动的文件>'

# 重建（改代码必须 --build）
ssh ... 'cd ~/flora_agent && docker compose --profile nginx --profile demo up -d --build agent agent-demo'
```

> **关键约定**（都是踩过坑的）：
> - 改**代码**必须 `--build`（代码 bake 进镜像，`restart` 无效）。
> - 改 `deploy/nginx.conf` 后必须 **`docker restart flora-nginx`** —— 单文件 bind mount，宿主机换文件后容器仍指向旧 inode，`nginx -s reload` **不生效**。
> - nginx 上游**一律用变量式 `proxy_pass`**。静态写法只在启动时解析并**永久缓存 IP**，容器重建换 IP 后会**静默打到别的实例**（2026-09-17 真实事故：生产请求跑进演示容器，表现为「API key 全失效」）。
> - 本地文件是 CRLF，传上去必须 `sed -i 's/\r$//'`，否则 nginx / Python 都可能报错。

**体验演示栈启停**：

```bash
docker compose --profile nginx --profile demo up -d --build   # 起（含演示库）
docker compose --profile demo down                            # 停（卷保留）
```

---

## 🛠️ 工具清单

### C 端可见（14 个）

| 类别 | 工具 | 简述 |
|---|---|---|
| 内容查询 | `retrieve_knowledge` | 查花艺知识库（花材 / 风格 / 搭配 / 预算 / 包装 / 养护） |
| 内容查询 | `platform_db_query_entity` | 平台只读查询（商品 / 店铺），**查询后自动渲染卡片** |
| 内容查询 | `search_history` | 跨会话检索该用户历史对话（按 `user_id` 隔离，只读） |
| 内容查询 | `get_user_profile` | 读长期偏好画像 |
| 设计 | `generate_diy_plan` | 生成 DIY 花束方案（锁店时原料限定该店可提供范围） |
| 设计 | `revise_diy_plan` | 按反馈改方案 |
| 设计 | `generate_effect_image` | 提交 AI 生图任务（按方案**真实支数**生成） |
| 产出 | `respond_to_user` | 纯文字回复 + 结构化理解信号（终结工具） |
| 产出 | `show_plan_card` | 出方案 / 商品卡（终结工具） |
| 产出 | `show_options` | 给用户一组可点选选项（终结工具） |
| 产出 | `suggest_greetings` | 推荐贺卡祝福语 |
| 产出 | `render_greeting_card` | 渲染贺卡图（5 模板） |
| 记忆 | `save_user_profile` / `save_memory` | 写入长期偏好 |

> **设计原则**：**任何「非文字输出」都做成独立工具**。能力若只藏在某个工具的**参数**里，模型想不到用
> —— 「出选项」原先只能靠 `respond_to_user` 手拼 `data`，实测模型从不主动调用；独立成 `show_options` 后立刻会用。

### 平台接入工具（8 个，`ops` 标签，默认对 C 端隐藏）

`platform_db_discover` · `platform_db_test_connection` · `platform_db_sample_table` ·
`platform_mapping_draft` · `platform_mapping_save_draft` · `platform_mapping_list_drafts` ·
`platform_mapping_set_status` · `platform_mapping_get_active`

> 仅部署接入期临时置 `ENABLE_OPS_TOOLS=true` 供配置方使用；**日常运营保持 false**（运维工具不走对话）。

完整定义见 `agent/toolkit.py`，运行时清单 = `agent.toolkit.get_tool_specs()`。

---

## 📡 API 速查

| 端点 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/health` | GET | 无 | 健康检查（容器探活） |
| `/auth/token` | POST | `X-API-Key` | 接入方换取 JWT |
| `/auth/anonymous` | POST | 无 | 匿名登录（**仅体验版开启**） |
| `/auth/wx-login` | POST | 微信凭证 | 微信登录 |
| `/auth/me` | GET | JWT | 查当前身份 |
| `/chat` | POST | JWT | 非流式对话（**必填 `user_id`**，须与 token 一致） |
| `/chat/stream` | POST | JWT | SSE 流式对话 |
| `/chat/reset` | POST | JWT | 清会话上下文 |
| `/conversations` | GET / POST | JWT | 列出 / 新建会话 |
| `/conversations/{id}/messages` | GET | JWT | 列消息 |
| `/tasks/{task_id}` | GET | JWT | 查生图任务（终态 `done`） |
| `/ui-contract` | GET | 无 | UI 卡片契约样例 |
| `/api/learning/order` | POST | webhook secret | 成交回调（成交即学；secret 留空即 503） |
| `/api/agent/query` | POST | 无（IP 限流） | 官网体验窗：单轮、纯规则引擎（不烧 LLM） |
| `/api/metrics/summary` | GET | `X-Dashboard-Key` | 24h 调用量 / 平均延迟 / 错误率 |
| `/api/metrics/calls` | GET | `X-Dashboard-Key` | 调用明细 |
| `/api/metrics/platforms` | GET | `X-Dashboard-Key` | 按平台分布 |
| `/api/metrics/tools` | GET | `X-Dashboard-Key` | 按工具分布 |
| `/api/metrics/stream` | GET | `X-Dashboard-Key` | SSE 实时调用流 |
| `/demo/` · `/demo-api/*` | — | nginx | 体验演示页静态资源 + 反代 `agent-demo` |

请求 / 响应 Schema、错误码、字段全部在 [`docs/04-API接口文档`](./docs/04-API接口文档.md)。

**对话请求的可选上下文字段**（锁店 / 入口判定）：

```json
{
  "message": "帮我配一束送妈妈的",
  "user_id": "twl_xxx",
  "session_id": null,
  "entry": "product",          // product | shop | home
  "product_id": "P001",
  "product_title": "星河长明",
  "shop_id": "s001"
}
```

> **锁店唯一来源 = 从商品详情页或店铺页进入**；占位值（如 `default`）一律视为未锁店。

---

## 🧪 测试与验收

| 层 | 内容 | 命令 |
|---|---|---|
| 单元 / 集成 | **432 条** pytest（含结构护栏、prompt 契约、卡片对齐、检索评测回归门） | `pytest -q` |
| 对话冒烟 | 40+ 条测试词按分组跑**真实对话**，自动比对产出类型 + 文本质量 | `python scripts/agent_smoke.py --group <组>` |
| 检索评测 | 50 条 golden queries，基线 hit@5 = 0.92 / MRR = 0.8667 | `python scripts/eval_retrieval.py` |

**测试词清单**（含每条期望产出、锁店传法、多轮序列）：`.workbuddy/artifacts/agent-test-phrases.md`。

冒烟脚本会检查两类问题：
1. **产出类型**（`plan_card` / `dialog_options` / `image_task` / `text`）是否符合预期；
2. **文本质量** —— 内部独白泄漏 / 工具名泄漏 / 交易引导词 / 模板占位符残留 / 空回复 / 原始数据行。

> ⚠️ **「ui 类型正确」≠「回复可用」**：内部独白泄漏时 `ui=text`、旧护栏全部"通过"，但整段是模型的自我推理。

---

## 📊 监控 & 运维

- **面板入口**：`https://api.tiaowulan.com/dashboard/`（密钥见服务器 `.env` 的 `DASHBOARD_API_KEY`）
- **指标**：24h 调用量、平均延迟、错误率、按平台 / 工具分布、实时调用流
- **关键命令**：
  ```bash
  docker ps                                                      # 看容器是否 healthy
  curl -s https://api.tiaowulan.com/health
  docker compose logs agent --since 10m | tail -100
  docker compose logs agent-demo --since 10m | tail -100         # ⚠️ 排查时务必对比两个实例
  ```
- **故障排查**：[`docs/08-运维监控手册 §5`](./docs/08-运维监控手册.md)
- **已知问题 + 历史修复**：[`docs/08 §7`](./docs/08-运维监控手册.md) · [`docs/11-变更记录`](./docs/11-变更记录.md)

**排查范式（值得记牢）**：

- 「**key 失效 / 接口 401**」→ 先比 **agent 与 agent-demo 两个容器的日志**，确认请求落点；大概率是 nginx 上游静态解析的老问题。
- 「**模型编造**（店名 / 价格 / 已出图）」→ 看日志有没有 `注入纠正` / `兜底`，判断是**没用工具**还是**工具失败**。
- 「**答非所问 / 卡在某个环节**」→ 查 `完成 阶段=` 与会话阶段是否反映了真实状态。

---

## 🧭 关键架构决策（**勿回退**）

| # | 决策 | 一句话说明 |
|---|---|---|
| 1 | **平台只读架构** | 商品 / 店铺实时只读查，本地不存不镜像不落。**订单明确不接** |
| 2 | **智能体不直接下单** | `create_order` 已移出工具注册表；切勿配下单地址 |
| 3 | **AI 贺卡模板化** | 5 套模板（PIL 合成），中文字体自动探测；水印固定「以花传情」 |
| 4 | **店铺锁定模式** | 从商品/店铺页进入才锁店；**代码层三层收口**（查询兜底过滤 / DIY 原料限定 / 不产店铺卡），不靠 prompt 自觉 |
| 5 | **`platform_id` = 身份隔离边界** | 同一业务方多端**共用一把** key（跨端记忆连续）；不同业务方**必须各一把**（否则用户撞库） |
| 6 | **理解主导，关键词仅兜底** | 需求抽取以 **LLM 理解为准**（正则只在模型没读到该字段时兜底）；**精确字段（花名 / 支数）仍以规则为准** |
| 7 | **prompt 只讲「怎么理解用户」** | 「不许做什么」交给**代码护栏**。场景已从"操作手册"改为"判断依据 + 禁用项"（体积 -27%） |
| 8 | **护栏四类，两条分支都守** | 未查证即作答 / 谎称已出图 / 该出卡只写文字 / 内部独白泄漏 —— **`respond_to_user` 分支与「模型完全不调工具」分支缺一不可** |
| 9 | **会话阶段必须反映事实** | 阶段不能变成"话题锁定"（曾因 `IMAGE_GEN` 永不退出，导致问数据库却被当生图话题回答） |
| 10 | **能力一律做成独立工具** | 藏在别人参数里的能力 = 对模型不可见 |
| 11 | **体验栈物理隔离** | 独立容器 + 独立库 + 独立密钥；范围收敛四层一起收口，写错时 **fail-closed** |
| 12 | **数据库结构不外泄** | prompt 硬规则 + `_strip_internal_leak` 确定性兜底（表名 / 字段名 / SQL / 连接串 → 中性说法） |

每条决策的背景与论证见 [`docs/03-系统架构设计`](./docs/03-系统架构设计.md) + [`docs/11-变更记录`](./docs/11-变更记录.md)。

---

## 🔐 安全与合规要点

| 要点 | 落地 |
|---|---|
| 多平台隔离 | 每个业务方持独立 `X-API-Key`；`user_id = f'{platform_id}_{sha256(platform_id:external_user_id)[:24]}'`，永不撞库 |
| JWT | HS256；`JWT_SECRET ≥ 32` 字符（生产强制） |
| CORS | 生产**必须明确白名单**，不使用 `*` |
| 匿名登录 | **仅体验版开启**；生产 `ANONYMOUS_LOGIN_ENABLED=False` |
| 平台数据只读 | `data_gateway/external.py` 强制 `SET TRANSACTION READ ONLY`；REST 源只走 GET |
| **数据库结构防泄露** | 回复中不出现表名 / 字段名 / 表关系 / SQL / 连接串；追问时用**业务语言**概括（prompt 硬规则 + 代码兜底） |
| **内部实现不外露** | 工具名 / 原始 JSON / 内部主键不出现在 `reply`（卡片 `data` 不受限） |
| 防编造 | 平台事实必须先查再答；未查到就如实说明，**不凭记忆编造店名 / 价格** |
| 异常回显 | LLM 调用失败对用户隐藏原始异常文案（仅 logger 落盘） |
| 密钥分层 | LLM / DB / 平台 / API key 各自从 `.env` 注入；`.env*` 已 gitignore，永不进仓 |
| 监控鉴权 | `/api/metrics/*` 走 `X-Dashboard-Key` |

完整信任模型与合规边界见 [`docs/10-安全与合规`](./docs/10-安全与合规.md)。

---

## 📚 完整文档索引

> **本节即为全仓库文档总索引**（含文档集地图、按角色推荐阅读路径与维护约定）。

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
| — | [接入方调用示例](./docs/接入方调用示例.md) | 接入方研发 |
| — | [小程序接入待办清单](./docs/小程序接入待办清单.md) | 小程序团队 |
| — | [知识库概览](./docs/知识库概览.md) | 产品 / 研发 |
| — | [知识库资料汇编](./docs/知识库资料汇编.md) | 内容运营 |
| — | [优化路线图](./docs/优化路线图.md) | 产品 / 研发 |

### 📖 按角色推荐阅读路径

| 角色 | 路径（先看本 README，再按序展开） |
|---|---|
| 🧭 新人交接 / 研发入门 | [01 项目概述](./docs/01-项目概述.md) → [03 系统架构设计](./docs/03-系统架构设计.md) → [04 API 接口文档](./docs/04-API接口文档.md) → [06 数据库设计](./docs/06-数据库设计.md) → [10 安全与合规](./docs/10-安全与合规.md) |
| 📱 小程序 / H5 / 官网接入 | [05 前端对接契约](./docs/05-前端对接契约.md) → [04 API 接口文档](./docs/04-API接口文档.md) → [接入方调用示例](./docs/接入方调用示例.md) → [小程序接入待办清单](./docs/小程序接入待办清单.md) |
| 🚀 部署 / 运维 / SRE | [07 部署手册](./docs/07-部署手册.md) → [08 运维监控手册](./docs/08-运维监控手册.md) → [11 变更记录](./docs/11-变更记录.md) |
| 🛡️ 安全 / 合规审计 | [10 安全与合规](./docs/10-安全与合规.md) → [03 系统架构设计 §ADR](./docs/03-系统架构设计.md) → [11 变更记录](./docs/11-变更记录.md) |
| 🔍 故障排查（oncall 直跳） | [08 运维监控手册 §5 故障排查](./docs/08-运维监控手册.md) + [§7 已知问题](./docs/08-运维监控手册.md) |
| 🧪 测试 / 验收 | [09 测试与验收](./docs/09-测试与验收.md) + `.workbuddy/artifacts/agent-test-phrases.md` |
| 📦 产品 / 需求管理 | [01 项目概述](./docs/01-项目概述.md) → [02 需求规格 (PRD)](./docs/02-需求规格(PRD).md) → [优化路线图](./docs/优化路线图.md) → [11 变更记录](./docs/11-变更记录.md) |

### 🗃️ 与归档旧文档的关系

历史散落的 README / DEPLOY / FRONTEND_CONTRACT 等文档已统一收口到 [`docs/archive/`](./docs/archive/)，**仅供追溯**。请一律以本文档集为准：

- `docs/archive/README.md` → 已被本 README + 01 + 03 取代
- `docs/archive/DEPLOY.md` / `docs/archive/广州ECS部署清单.md` → 已被 [07 部署手册](./docs/07-部署手册.md) 取代
- `docs/archive/FRONTEND_CONTRACT.md` → 已被 [05 前端对接契约](./docs/05-前端对接契约.md) 取代
- `docs/archive/接入说明-花艺智能体API.md` / `docs/archive/服务接入8问答.md` → 已被 [04 API 接口文档](./docs/04-API接口文档.md) 取代

> ⚠️ 归档中若有「LLM = 腾讯 hy」「生图仅 mock」「平台只读库」等表述均为**过时信息**：
> 现用阿里云百炼 Qwen；平台数据当前走**只读 REST**（`aistore.xiangbinmeigui.com`）。

### 📐 文档维护约定

- **结论取自代码**：架构、接口、配置以仓库源码为权威；引用格式 `文件:行号` 或 `git HEAD <commit>`。
- **可运行数字**：工具数量等以**运行时实际状态**为准（`agent.toolkit.get_tool_specs()`）。
- **变更同步**：架构或接口级变更须在 [11 变更记录](./docs/11-变更记录.md) 增条目，并同步本 README 的「项目快照」。
- **命名约定**：历史项目代号只用于内部资产标识（容器名、域名、日志前缀），**不是对外品牌名**。

### 🧾 文档集版本线

| 日期 | 文档集版本 | 主要变化 |
|---|---|---|
| **2026-09-17** | **v2.2** | README 对齐当前状态：6 服务（含体验栈）、工具面（22 注册 / 14 可见）、真实端点清单、测试体系（432 + 冒烟）、新增 6 条架构决策、路线图补 09-08~09-17 |
| 2026-09-07 | v2.1（文档唯一化） | `docs/README.md` 废除，入口并入根 `README.md` |
| 2026-09-07 | v2.0 | 新建仓库根 `README.md`；K-1~K-3、T-1~T-4 全部修复 |
| 2026-09-07 | v1.0 | 首次按标准模板建立文档集，旧文档归档 |
| 2026-09-04 及之前 | 散落文档 | 已归档 |

---

## 🗺️ 路线图

**已完成（2026-09-08 ~ 09-17）**

| 日期 | 事项 |
|---|---|
| 09-15 | 生产上线（`api.tiaowulan.com`，容器 healthy + 监控面板活跃） |
| 09-15~16 | 健壮性加固：流式挂起 / 参数 JSON / 卡片空文本 / 生图超时 / 并发护栏 |
| 09-16 | **平台只读 REST 接入**（`aistore.xiangbinmeigui.com`），解除「平台只读库」硬阻断 |
| 09-16 | 对话理解 + 需求记忆 + 出卡护栏（含 else 分支 + 规则引擎兜底）+ 支数一致性 |
| 09-16 | 商品卡与文案对齐去重；效果图提示词带**真实支数** |
| 09-16 | **隔离体验栈**上线 + 范围收敛（只做方案与建议，无店铺/下单）+ 写错 fail-closed |
| 09-16 | 首轮「该出卡却只写文字」护栏生效 + 体验版交易引导兜底 |
| 09-16 | 演示页「无法连接到服务器」根治（BASE 三级自适应 + CORS） |
| 09-17 | **项目改名定名**：`platform_id: test → twl`（趁无真实用户定名） |
| 09-17 | **「机械走流程」治理**：阶段粘滞修复 + prompt 去脚本化（体积 -27%） |
| 09-17 | **理解主导改造**：需求抽取以 LLM 理解为准（正则仅兜底） |
| 09-17 | **文字之外全部工具化**：新增 `show_options`；卡片优先于生图 |
| 09-17 | **锁店 DIY 原料对齐**：按店铺在售商品推断可提供花材，缺料保留 + 标注 |
| 09-17 | **数据库结构防泄露** + 内部独白泄漏护栏 |
| 09-17 | **nginx 上游变量化**：根治「容器重建后请求打到别的实例」（表现为 API key 失效） |
| 09-17 | 官网体验窗 `www.tiaowulan.com/agent.html` 后端就绪（待站点方覆盖 `agent-client.js`） |
| 09-17 | ICP 备案通过；对话冒烟测试体系上线（432 测试 + 40+ 测试词） |

**进行中**

- 🚧 小程序对接：流式输出改造 / DIY 卡片 / 历史会话（[`docs/小程序接入待办清单`](./docs/小程序接入待办清单.md)）
- 🚧 站点方覆盖 `assets/js/agent-client.js`（`MODE:'live'`），官网体验窗正式对外

**待优化**

- ⏸️ 工具链 async 化（当前单轮 **38–51s**，工具调用串行）
- embedding 通道真机标定（当前默认关，纯 TF-IDF 兜底）
- 多语言 / 国际化
- 企业微信 / 公众号接入

**待平台方配合**

- 开「花材库」只读接口（现有花材 ID **无对应名称**，锁店原料只能从商品文案推断）
- 收口 `/v1/merchant/dashboard` 与 `/shops` 的匿名可读敏感字段

**外部依赖提醒**

- TLS 证书 **2026-12-01 到期**（阿里云免费证书）
- `DASHBOARD_API_KEY` 建议换更长密钥

---

## 📮 反馈与维护

- **仓库 owner / 维护人**：见 [`docs/01-项目概述 §1`](./docs/01-项目概述.md)
- **报送问题**：内部 IM / GitHub Issue / 走运维故障排查手册流程
- **内部代号说明**：`flora_agent`（仓库名）/ `tiaowulan`（域名）/ Docker 容器前缀 `flora-` —— 这些词**只用于内部资产标识，不是对外品牌名**。对外一律称「花艺智能体」或运行时自称「你的专属花艺小助手」。

---

## 📝 版权与许可

仅供合作方内部使用，合同授权范围外禁止外发、二次分发或反向工程。

---

> **项目状态**：🟢 在跑 · **最后核验**：2026-09-17（432 测试全绿 · 生产 + 体验栈均 healthy）
