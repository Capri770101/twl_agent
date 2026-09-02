# 花艺智能体 —— 部署配置指南

本文档涵盖所有需要配置的项，按模块分类。

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
# 或 SQLite（开发环境）：
# DATABASE_URL=sqlite:///./data/agent.db

# ═══════════════════════════════════════════════════════════
# 3. JWT 鉴权
# ═══════════════════════════════════════════════════════════
JWT_SECRET=your-random-secret-key-change-this
JWT_EXPIRE_HOURS=720            # token 有效期（小时）

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
WX_APPID=wx1234567890abcdef     # 微信小程序 AppID
WX_SECRET=your-wx-secret        # 微信小程序 AppSecret

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

### 2.3 微信小程序配置

| 变量 | 必填 | 说明 |
|------|------|------|
| `WX_APPID` | ✅ | 小程序 AppID（微信公众平台获取） |
| `WX_SECRET` | ✅ | 小程序 AppSecret |

**获取方式：**
1. 登录 https://mp.weixin.qq.com
2. 开发 → 开发管理 → 开发设置
3. 复制 AppID 和 AppSecret

### 2.4 数据库配置

| 变量 | 必填 | 说明 |
|------|------|------|
| `DATABASE_URL` | ✅ | 数据库连接串 |

**PostgreSQL（生产推荐）：**
```
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

**SQLite（开发/测试）：**
```
DATABASE_URL=sqlite:///./data/agent.db
```

### 2.5 腾讯地图（可选）

| 变量 | 必填 | 说明 |
|------|------|------|
| `TENCENT_MAP_KEY` | ❌ | 用于距离计算和逆地理编码 |

**获取方式：**
1. 登录 https://lbs.qq.com
2. 创建应用 → 添加 Key
3. 选择 WebService API

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
A: 检查 `WX_APPID` 和 `WX_SECRET` 是否与小程序后台一致。

### Q: 数据库连接失败？
A: 检查 `DATABASE_URL` 格式。PostgreSQL 需要安装 `asyncpg`；SQLite 需要创建 `data/` 目录。

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
