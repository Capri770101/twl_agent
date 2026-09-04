# 花艺智能体 —— 部署配置指南

本文档涵盖所有需要配置的项，按模块分类。

> ⚠️ **前端对接必读（重要）**
>
> 本包是**纯后端 API**，只产出结构化 `ui / data / action`，**前端渲染由宿主平台自行负责**。接入前请确认宿主前端**已经具备**以下组件，否则会出现「智能体返回了方案数据、前端却没地方渲染」的断层：
>
> - `text` 文本气泡（最低要求，必须有）
> - `dialog_options` 选项按钮
> - `plan_card` 方案卡片（展示 + 确认/修改按钮）
> - `shop_card` 店铺卡片
> - `order_card` 订单确认卡
> - `pay_jump` 支付跳转（打开平台自己的支付页）
> - `image_task` 生图进度（含 `GET /tasks/{task_id}` 轮询）
>
> 每个 UI 类型的数据结构、渲染要求与示例见 **[FRONTEND_CONTRACT.md](FRONTEND_CONTRACT.md)**（接入必读）；也可调用 `GET /ui-contract` 拉取机器可读清单程序化对照。能力缺失时请按 `action.fallback` / `reply` 做文本降级，不要报错中断。

> **非 UI 生图依赖也必须满足**：生产环境需要 PostgreSQL、`psycopg[binary]`、可写的图片目录或对象存储/CDN。生图任务状态持久化在 `image_tasks` 表；图片文件默认写入 `data/generated/`。使用 CDN/对象存储时请配置 `IMAGE_PUBLIC_BASE_URL`，并设置对象存储生命周期清理规则。

> **鉴权必读**：本包提供 `POST /auth/token`（通用平台接入，请求头 `X-API-Key` + 请求体 `external_user_id`）、`POST /auth/anonymous`、`POST /auth/wx-login`、`GET /auth/me`。生产环境必须配置至少 32 位 `JWT_SECRET`，服务会强制 Bearer 鉴权；多平台接入建议同时配置 `PLATFORM_API_KEYS`，匿名登录在生产环境默认关闭。微信配置推荐使用 `WECHAT_APPID` / `WECHAT_SECRET`，同时兼容 `WX_APPID` / `WX_SECRET`。

> **数据库没有 `image_tasks` 表怎么办？** 有建表权限时，服务启动会通过 `CREATE TABLE IF NOT EXISTS` 自动创建；生产数据库通常由 DBA 管理、应用账号没有 DDL 权限时，请先执行仓库中的 [`migrations/001_image_tasks.sql`](migrations/001_image_tasks.sql)，再启动服务。启动自检失败会直接提示迁移文件路径。

---

## 一、环境变量（.env）

复制 `.env.example` 为 `.env`，填入以下配置：

```bash
# ═══════════════════════════════════════════════════════════
# 1. 服务配置
# ═══════════════════════════════════════════════════════════
APP_ENV=prod                    # dev / prod
HOST=0.0.0.0                   # 监听地址
PORT=8000                      # 监听端口

# ═══════════════════════════════════════════════════════════
# 2. 数据库
# ═══════════════════════════════════════════════════════════
# 方式一：使用 docker-compose 自带的 postgres 容器（host 固定写 postgres）
DATABASE_URL=postgresql://flora:密码@postgres:5432/flora_agent
POSTGRES_PASSWORD=数据库容器密码          # 仅供 compose 中 postgres 容器使用
# 方式二：外部/云数据库，直接写实际地址，POSTGRES_PASSWORD 可留空
# DATABASE_URL=postgresql://user:pass@host:5432/flora_agent
# 生产和多实例部署必须使用 PostgreSQL；禁止 SQLite

# ═══════════════════════════════════════════════════════════
# 3. JWT 鉴权
# ═══════════════════════════════════════════════════════════
JWT_SECRET=replace-with-a-random-secret-at-least-32-characters
JWT_EXPIRE_HOURS=720            # token 有效期（小时）
AUTH_REQUIRED=true              # 生产环境会强制为 true

# ── 多平台接入（推荐配置）──
# 平台级 API Key，格式 "platform_id=key"，多个用逗号或换行分隔。
# 接入方后端认证完自己的用户后，携带 X-API-Key 调 POST /auth/token 换取智能体 token。
# 生成方式：python -c "import secrets; print(secrets.token_urlsafe(32))"
PLATFORM_API_KEYS=wxmini=sk-REPLACE-1,h5app=sk-REPLACE-2
# 匿名登录仅用于开发联调；生产未显式设置时自动关闭
# ANONYMOUS_LOGIN_ENABLED=false

# ═══════════════════════════════════════════════════════════
# 4. LLM 大模型（必填）
# ═══════════════════════════════════════════════════════════
LLM_API_KEY=sk-xxx              # OpenAI / 兼容 API Key
LLM_BASE_URL=https://api.openai.com/v1   # API 地址
LLM_MODEL=gpt-4o-mini           # 模型名称
LLM_MAX_ITERATIONS=8            # 单轮对话最大工具调用次数
LLM_REQUEST_TIMEOUT=120         # 单次 LLM 调用超时（秒）

# ═══════════════════════════════════════════════════════════
# 5. 文生图模型（效果图生成）
# ═══════════════════════════════════════════════════════════
IMAGE_PROVIDER=mock             # mock / hy
IMAGE_API_KEY=
IMAGE_BASE_URL=
IMAGE_MODEL=
IMAGE_WIDTH=768
IMAGE_HEIGHT=1024
IMAGE_PUBLIC_BASE_URL=

# ═══════════════════════════════════════════════════════════
# 6. 微信小程序（登录）
# ═══════════════════════════════════════════════════════════
WECHAT_APPID=wx1234567890abcdef
WECHAT_SECRET=your-wx-secret

# ═══════════════════════════════════════════════════════════
# 7. 目标平台外部数据源（按 source_id 配置）
# ═══════════════════════════════════════════════════════════
# PLATFORM_DB_MAIN_URL=postgresql://readonly_user:password@platform-db:5432/platform

# ═══════════════════════════════════════════════════════════
# 8. 腾讯地图（可选）
# ═══════════════════════════════════════════════════════════
TENCENT_MAP_KEY=your-map-key

# ═══════════════════════════════════════════════════════════
# 9. Redis（可选，用于限流 / 缓存）
# ═══════════════════════════════════════════════════════════
REDIS_URL=redis://localhost:6379/0

# ═══════════════════════════════════════════════════════════
# 10. CORS（允许的小程序/网页域名）
# ═══════════════════════════════════════════════════════════
ALLOWED_ORIGINS=https://your-miniprogram.com,https://your-h5.com
```

---

## 二、当前数据库接入模型

- **内部控制面**：使用 `DATABASE_URL` 指向智能体自己的 PostgreSQL，保存会话、消息、记忆、任务、映射草案与审计日志。
- **外部目标平台库**：按 `PLATFORM_DB_<SOURCE_ID>_URL` 配置，只读发现、脱敏样本和已审批映射查询。
- **SQLite**：生产环境禁止使用。

### 2.1 外部数据库接入流程

1. 配置 `PLATFORM_DB_<SOURCE_ID>_URL`
2. 调用 `platform_db_test_connection(source_id)`
3. 调用 `platform_db_discover(source_id, sample_rows=0)`
4. 必要时调用 `platform_db_sample_table(source_id, schema, table, limit)`
5. 调用 `platform_mapping_draft(profile)` 生成草案
6. 调用 `platform_mapping_save_draft(profile, draft)` 保存版本
7. 使用 `platform_mapping_set_status(...)` 完成 reviewed / approved / active
8. 通过 `platform_db_query_entity(source_id, entity)` 查询业务实体

### 2.2 目标平台数据库说明

- `platform_db_discover` 只支持外部 PostgreSQL 连接器
- 所有外部连接均为只读事务
- 样本默认不读取，最大 5 行
- `platform_db_query_entity` 只允许查询 active 映射
- 没有 active 映射时直接拒绝查询

---

## 三、数据库与生图迁移

如果部署方使用已有业务数据库，不要求覆盖平台现有表。建议按以下顺序操作：

1. DBA 检查目标 PostgreSQL 数据库和应用账号。
2. 若应用账号有建表权限，直接启动服务，`init_db()` 会创建缺失表。
3. 若应用账号没有建表权限，DBA 执行 [`migrations/001_image_tasks.sql`](migrations/001_image_tasks.sql)，并确认应用账号拥有 `image_tasks` 的 `SELECT`、`INSERT`、`UPDATE` 权限。
4. 启动服务，确认日志通过数据库初始化和 `image_tasks` 自检。
5. 用 `POST /chat` 触发生图，再用 `GET /tasks/{task_id}` 验证状态能查询。

---

## 四、支持的图像生成提供商

| 提供商 | 说明 |
|---|---|
| `mock` | 仅用于开发联调 |
| `hy` | 当前已实现 |

> 其他 provider 目前仅保留配置占位，未在当前代码中实现。

---

## 五、API 一致性说明

当前代码中的 `/chat` 返回字段为：`reply`、`ui`、`data`、`action`、`tool_calls`、`session_id`、`stage`。

`/chat/stream` 使用 SSE，真实事件（以线上实测为准）为：`tool_call`、`text`、`card`、`done`、`error`。注意：实测中没有 `thinking` 和 `tool_result` 这两个事件。

---

## 六、部署检查

启动后至少验证：

- `GET /health`
- `POST /auth/token`（带 `X-API-Key`，验证平台接入链路）
- `POST /chat`
- `POST /chat/stream`
- `GET /tasks/{task_id}`
- `GET /ui-contract`

---

## 七、补充

完整配置请参考 [.env.example](.env.example) 和 [README.md](README.md)。
      shop_id: shopId  // 进入店铺时传入，锁定后整个会话不变
    }
  });
}

// SSE 流式对话
function chatStream(message, conversationId, shopId) {
  const token = wx.getStorageSync('token');
  const task = wx.connectSocket({
    url: `wss://your-domain.com/chat/stream?token=${token}`
  });
  // ... 处理 SSE 事件
}
```

### 5.3 会话管理

```javascript
// 获取会话列表
async function getConversations() {
  const token = wx.getStorageSync('token');
  return await wx.request({
    url: 'https://your-domain.com/conversations',
    header: { Authorization: `Bearer ${token}` }
  });
}

// 获取会话消息
async function getMessages(convId) {
  const token = wx.getStorageSync('token');
  return await wx.request({
    url: `https://your-domain.com/conversations/${convId}/messages`,
    header: { Authorization: `Bearer ${token}` }
  });
}
```

---

## 六、API 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/auth/token` | 通用平台接入：`X-API-Key` + `external_user_id` 换 token |
| `POST` | `/auth/wx-login` | 微信登录（code 换 token） |
| `POST` | `/auth/anonymous` | 匿名登录（仅开发联调，生产默认关闭） |
| `GET` | `/auth/me` | 当前用户信息 |
| `POST` | `/chat` | 对话（同步） |
| `POST` | `/chat/stream` | 对话（SSE 流式） |
| `POST` | `/chat/reset` | 删除会话 |
| `GET` | `/conversations` | 会话列表 |
| `GET` | `/conversations/{id}/messages` | 会话消息 |
| `GET` | `/tasks/{task_id}` | 生图任务状态 |
| `GET` | `/generated/{task_id}.png` | 生图结果（静态托管） |
| `GET` | `/ui-contract` | UI 契约（接入方对照组件清单） |
| `GET` | `/health` | 健康检查 |

### 多平台接入流程

各平台统一走「平台 API Key + 用户标识」：

1. 部署方在 `PLATFORM_API_KEYS` 为每个接入平台配置 `platform_id=key`。
2. 接入方后端认证自己的终端用户（各平台用自己的登录体系）。
3. 接入方后端携带 `X-API-Key` 请求 `POST /auth/token`，body 为 `{"external_user_id": "该平台体系内的用户标识"}`。
4. 拿返回的 `access_token` 调用 `/chat` 等业务接口，`user_id` 使用返回值（`platform_id + external_user_id` 哈希派生，各平台用户天然隔离）。

微信小程序可选两条路：走上面的通用流程，或使用内置 `/auth/wx-login`（智能体直接调微信 `jscode2session`，需配置 `WECHAT_APPID` / `WECHAT_SECRET`）。

---

## 七、常见问题

### Q: LLM 调用报错 401？
A: 检查 `LLM_API_KEY` 是否正确，是否过期。

### Q: 文生图不生成？
A: 检查 `IMAGE_API_KEY` 和 `IMAGE_PROVIDER` 是否配置。不配置则跳过生图功能。

### Q: 微信登录失败？
A: 检查 `WECHAT_APPID` 和 `WECHAT_SECRET` 是否与小程序后台一致；旧配置名 `WX_APPID` 和 `WX_SECRET` 仍兼容。

### Q: 登录接口返回 401/503？
A: 确认已配置至少 32 位 `JWT_SECRET`。微信登录还需要配置 `WECHAT_APPID` 和 `WECHAT_SECRET`；匿名登录不需要微信配置，但仍需要 `JWT_SECRET`。

### Q: 数据库连接失败？
A: 检查 `DATABASE_URL` 是否为可访问的 PostgreSQL 地址、用户名/密码/端口是否正确，以及部署环境是否允许访问数据库。驱动由 `psycopg[binary]` 提供；生产环境不支持 SQLite。

### Q: 小程序请求被 CORS 拦截？
A: 检查 `ALLOWED_ORIGINS` 是否包含小程序域名（不含路径）。

### Q: 工具调用超时？
A: 调大 `LLM_REQUEST_TIMEOUT`（默认 120 秒）。

### Q: 如何添加新知识？
A: 编辑 `agent/knowledge/` 下的 JSON 文件，重启服务即可。格式参考 `flowers.json`。

### Q: 如何添加新工具？
A: 在 `agent/tools.py` 中添加工具定义和执行函数，重启服务即可。

---

## 八、知识库扩充规则

当前知识库采用「核心本地库 + 扩展来源」的方案。

### 8.1 核心本地库

以下文件是智能体默认直接读取的本地知识：

- `agent/knowledge/flowers.json`
- `agent/knowledge/styles.json`
- `agent/knowledge/scenes.json`
- `agent/knowledge/pairings.json`
- `agent/knowledge/budget.json`
- `agent/knowledge/packaging.json`

### 8.2 扩展来源

以下来源用于后续扩充，不建议直接混进核心库：

- `PlantFlowerDatasets`
- `flower-db`
- `Flower-Knowledge-Graph-Visualization`
- `flora-atlas`

### 8.3 入库原则

1. 先标准化字段，再写入本地。
2. 核心库保留高频、稳定、业务强相关内容。
3. 扩展源优先写入 `agent/knowledge/sources/` 下的标准化文件。
4. 经过验证的高质量内容，再从 sources 合并回核心 JSON。
5. 所有新增数据都应带 `source` 字段，便于追溯。

### 8.4 推荐字段

```json
{
  "id": "F_ROSE_EXT",
  "name": "玫瑰",
  "aliases": ["红玫瑰", "粉玫瑰"],
  "tags": ["主花", "浪漫"],
  "source": "PlantFlowerDatasets",
  "flower_language": ["爱情", "浪漫"],
  "care": {
    "light": "充足散射光",
    "water": "见干见湿"
  },
  "season": ["四季"],
  "colors": ["红", "粉"],
  "pairing_notes": "适合搭配满天星和尤加利"
}
```

### 8.5 导入脚本

脚本入口：`scripts/import_flower_knowledge.py`

作用：
- 读取知识库清单
- 统一外部记录格式
- 为后续批量灌入预留入口

目前脚本只负责标准化入口，后续可以继续扩展成：
- 拉取 GitHub/数据集文件
- 解析 JSONL / CSV
- 生成核心知识 JSON

---

## 九、封装边界说明

这个包已经封装好的部分：

- 智能体主逻辑 `agent/`
- 本地知识库 `agent/knowledge/`
- 基础后端存储结构 `backend/storage/`
- 本地测试入口 `scripts/test_agent_local.py`
- 部署入口 `main.py`
- Docker 与说明文档

需要部署方自行配置的部分：

- LLM API Key / Base URL / Model
- 文生图 API Key
- 微信小程序 AppID / Secret
- 数据库实际连接信息
- 生产域名、CORS、回调地址
- Redis / 对象存储 / 地图服务等可选外部资源

原则：

1. 我们负责封装代码和默认能力。
2. 部署方负责填充生产凭据和基础设施地址。
4. 平台若暂未实现“方案页展示 / 订单页 / 支付跳转”等能力，可先按智能体返回的 `action` 做文本降级，不要阻断完整业务流程。
5. 平台接入时应优先检查 `required_capabilities`，按能力表逐步补齐前端页面与业务接口。
