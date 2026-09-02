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

> **鉴权必读**：本包提供 `POST /auth/anonymous`、`POST /auth/wx-login`、`GET /auth/me`。生产环境必须配置至少 32 位 `JWT_SECRET`，服务会强制 Bearer 鉴权；不要再仅凭请求体里的 `user_id` 识别用户。微信配置推荐使用 `WECHAT_APPID` / `WECHAT_SECRET`，同时兼容 `WX_APPID` / `WX_SECRET`。

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
DATABASE_URL=postgresql://user:pass@localhost:5432/flora_agent
# 生产和多实例部署必须使用 PostgreSQL；禁止 SQLite

# ═══════════════════════════════════════════════════════════
# 3. JWT 鉴权
# ═══════════════════════════════════════════════════════════
JWT_SECRET=replace-with-a-random-secret-at-least-32-characters
JWT_EXPIRE_HOURS=720            # token 有效期（小时）
AUTH_REQUIRED=true              # 生产环境会强制为 true

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
IMAGE_PROVIDER=flux             # flux / dall-e / kling / comfyui
IMAGE_API_KEY=your-image-api-key
IMAGE_BASE_URL=https://api.image-provider.com/v1
IMAGE_MODEL=flux-schnell        # 模型名称
IMAGE_WIDTH=768                 # 生成图片宽度
IMAGE_HEIGHT=1024               # 生成图片图片高度

# ═══════════════════════════════════════════════════════════
# 6. 微信小程序（登录 + 推送）
# ═══════════════════════════════════════════════════════════
WECHAT_APPID=wx1234567890abcdef # 微信小程序 AppID
WECHAT_SECRET=your-wx-secret    # 微信小程序 AppSecret

# ═══════════════════════════════════════════════════════════
# 7. 腾讯地图（距离计算 / 逆地理编码）
# ═══════════════════════════════════════════════════════════
TENCENT_MAP_KEY=your-map-key    # 腾讯地图 WebService Key

# ═══════════════════════════════════════════════════════════
# 8. Redis（可选，用于限流 / 缓存）
# ═══════════════════════════════════════════════════════════
REDIS_URL=redis://localhost:6379/0
# 不填则禁用 Redis 限流（开发环境可不配）

# ═══════════════════════════════════════════════════════════
# 9. CORS（允许的小程序/网页域名）
# ═══════════════════════════════════════════════════════════
ALLOWED_ORIGINS=https://your-miniprogram.com,https://your-h5.com
# * 表示允许所有来源（仅开发环境使用）
```

视觉 MCP 还可配置：`ZHIPU_API_KEY`、`VISION_ALLOWED_ROOT`（默认 `data/generated`）和 `VISION_MAX_IMAGE_BYTES`（默认 10 MiB）。不要把视觉 MCP 暴露到公网；本地路径读取和外部 URL 请求均应只在受信任的 MCP host 内使用。

---

## 二、各配置项详解

### 2.1 LLM 大模型配置

| 变量 | 必填 | 说明 |
|------|------|------|
| `LLM_API_KEY` | ✅ | OpenAI 兼容 API Key |
| `LLM_BASE_URL` | ✅ | API 地址（OpenAI/DeepSeek/本地部署都行） |
| `LLM_MODEL` | ✅ | 模型名称 |
| `LLM_MAX_ITERATIONS` | ❌ | 单轮最大工具调用次数，默认 8 |
| `LLM_REQUEST_TIMEOUT` | ❌ | 超时秒数，默认 120 |

**支持的 LLM 提供商：**
- OpenAI：`https://api.openai.com/v1` / `gpt-4o-mini`
- DeepSeek：`https://api.deepseek.com/v1` / `deepseek-chat`
- 通义千问：`https://dashscope.aliyuncs.com/compatible-mode/v1` / `qwen-plus`
- 本地 Ollama：`http://localhost:11434/v1` / `qwen2.5:7b`

### 2.2 文生图配置

| 变量 | 必填 | 说明 |
|------|------|------|
| `IMAGE_PROVIDER` | ✅ | 提供商：flux / dall-e / kling / comfyui |
| `IMAGE_API_KEY` | ✅ | API Key |
| `IMAGE_BASE_URL` | ✅ | API 地址 |
| `IMAGE_MODEL` | ✅ | 模型名称 |
| `IMAGE_WIDTH` | ❌ | 图片宽度，默认 768 |
| `IMAGE_HEIGHT` | ❌ | 图片高度，默认 1024 |

**支持的文生图提供商：**
- Flux：`https://api.bfl.ml/v1` / `flux-schnell`
- DALL-E：`https://api.openai.com/v1` / `dall-e-3`
- 可灵：`https://api.klingai.com/v1` / `kling-v1`
- ComfyUI：`http://localhost:8188`（本地部署）

> 注：当前代码已实现 `mock` / `hy`（`backend/storage/tasks.py`），其余为预留声明，接入前需补充实现。生图结果存本地 `data/generated/`，经 `/generated` 静态挂载访问；生产可用 `IMAGE_PUBLIC_BASE_URL` 指定 CDN/对象存储公网前缀。

### 2.3 微信小程序配置

| 变量 | 必填 | 说明 |
|------|------|------|
| `WECHAT_APPID` | ✅ | 小程序 AppID（兼容 `WX_APPID`） |
| `WECHAT_SECRET` | ✅ | 小程序 AppSecret（兼容 `WX_SECRET`） |

**获取方式：**
1. 登录 https://mp.weixin.qq.com
2. 开发 → 开发管理 → 开发设置
3. 复制 AppID 和 AppSecret

### 2.4 数据库配置

| 变量 | 必填 | 说明 |
|------|------|------|
| `DATABASE_URL` | ✅ | 数据库连接串 |

**PostgreSQL：**
- 驱动：`psycopg[binary]`，已包含在 `requirements.txt`
- 生产环境必须配置 `DATABASE_URL=postgresql://...`
- 不支持 SQLite 作为服务数据库

### 2.5 腾讯地图（可选）

| 变量 | 必填 | 说明 |
|------|------|------|
| `TENCENT_MAP_KEY` | ❌ | 用于距离计算和逆地理编码 |

**获取方式：**
1. 登录 https://lbs.qq.com
2. 创建应用 → 添加 Key
3. 选择 WebService API

### 数据库迁移顺序

如果部署方使用已有业务数据库，不要求覆盖平台现有表。建议按以下顺序操作：

1. DBA 检查目标 PostgreSQL 数据库和应用账号。
2. 若应用账号有建表权限，直接启动服务，`init_db()` 会创建缺失表。
3. 若应用账号没有建表权限，DBA 执行 [`migrations/001_image_tasks.sql`](migrations/001_image_tasks.sql)，并确认应用账号拥有 `image_tasks` 的 `SELECT`、`INSERT`、`UPDATE` 权限。
4. 启动服务，确认日志通过数据库初始化和 `image_tasks` 自检。
5. 用 `POST /chat` 触发生图，再用 `GET /tasks/{task_id}` 验证状态能查询。

### 2.6 生图结果存储与任务持久化

| 配置/资源 | 必填 | 说明 |
|---|---|---|
| `IMAGE_PUBLIC_BASE_URL` | 本地静态托管可不填 | 生产推荐配置 CDN/对象存储的公网 URL 前缀 |
| `data/generated/` | 本地模式必填 | 运行用户必须有创建目录和写入 PNG 的权限 |
| PostgreSQL `image_tasks` | 生产必填 | 保存任务状态、提示词、结果 URL 和失败原因 |

任务状态由数据库持久化，`GET /tasks/{task_id}` 可跨进程查询。服务重启时尚未完成的 `processing` 任务会被标记为 `failed`，需要重新提交；已经 `done` 的记录仍可查询，但对应图片文件或对象存储对象也必须保留。文件清理应由平台定时任务或对象存储生命周期规则负责。

---

## 三、Docker 部署

```bash
# 1. 复制配置
cp .env.example .env
vim .env  # 填入配置

# 2. 启动服务
docker-compose up -d

# 3. 查看日志
docker-compose logs -f agent

# 4. 健康检查
curl http://localhost:8000/health
```

---

## 四、裸机部署

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制配置
cp .env.example .env
vim .env

# 3. 启动服务
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# 4. 或后台运行
nohup python -m uvicorn main:app --host 0.0.0.0 --port 8000 > agent.log 2>&1 &
```

---

## 五、小程序接入

### 5.1 登录接口

```javascript
// 小程序登录
wx.login({
  success: async (res) => {
    const { data } = await wx.request({
      url: 'https://your-domain.com/auth/wx-login',
      method: 'POST',
      data: { code: res.code }
    });
    wx.setStorageSync('token', data.token);
  }
});
```

### 5.2 对话接口

```javascript
// 发送消息
async function chat(message, conversationId, shopId) {
  const token = wx.getStorageSync('token');
  return await wx.request({
    url: 'https://your-domain.com/chat',
    method: 'POST',
    header: { Authorization: `Bearer ${token}` },
    data: {
      message,
      conversation_id: conversationId,
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
| `POST` | `/auth/wx-login` | 微信登录（code 换 token） |
| `POST` | `/auth/anonymous` | 匿名登录 |
| `GET` | `/auth/me` | 当前用户信息 |
| `POST` | `/chat` | 对话（同步） |
| `POST` | `/chat/stream` | 对话（SSE 流式） |
| `POST` | `/chat/reset` | 删除会话 |
| `GET` | `/conversations` | 会话列表 |
| `GET` | `/conversations/{id}/messages` | 会话消息 |
| `GET` | `/health` | 健康检查 |

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
