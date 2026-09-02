# 🌸 Flora Agent - 花艺智能体

基于 ReAct 架构的花艺顾问 AI 系统，支持微信小程序接入，提供专业的花艺设计建议和效果图像生成。

## 📋 目录

- [项目特性](#项目特性)
- [系统架构](#系统架构)
- [快速开始](#快速开始)
- [环境配置](#环境配置)
- [API 接口文档](#api-接口文档)
- [微信小程序接入](#微信小程序接入)
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
- **Docker 一键部署**：提供完整的容器化部署方案

## 🏗️ 系统架构

```
flora_agent_package/
├── main.py                    # FastAPI 服务入口
├── agent/                     # 智能体核心模块
│   ├── agent.py              # ReAct 智能体实现
│   ├── tools.py              # 工具函数定义
│   ├── engine/               # 推理引擎
│   ├── knowledge/            # 知识库
│   │   ├── flowers.json      # 花材知识
│   │   ├── styles.json       # 花艺风格
│   │   ├── scenes.json       # 应用场景
│   │   ├── pairings.json     # 搭配建议
│   │   ├── budget.json       # 预算方案
│   │   └── packaging.json    # 包装建议
│   ├── skills/               # 技能模块
│   └── mcp_servers/          # MCP 服务
├── backend/                   # 后端服务
│   ├── config.py             # 配置管理
│   ├── routers/              # API 路由
│   │   └── chat.py           # 对话接口
│   ├── storage/              # 存储层
│   └── data_gateway/         # 数据网关
├── scripts/                   # 脚本工具
│   ├── import_flower_knowledge.py  # 知识导入脚本
│   └── test_agent_local.py         # 本地测试脚本
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
| `DATABASE_URL` | 数据库连接串 | SQLite |
| `IMAGE_PROVIDER` | 图像生成提供商 | `mock` |
| `WX_APPID` | 微信小程序 AppID | - |
| `WX_SECRET` | 微信小程序 AppSecret | - |
| `TENCENT_MAP_KEY` | 腾讯地图密钥 | - |
| `REDIS_URL` | Redis 连接串 | - |
| `ALLOWED_ORIGINS` | CORS 允许域名 | `*` |

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
| hy 大模型（推荐） | `https://tokenhub.tencentmaas.com/v1/responses` | `Hy-Image-3.0` |
| Flux | `https://api.bfl.ml/v1` | `flux-schnell` |
| DALL-E | `https://api.openai.com/v1` | `dall-e-3` |
| 可灵 | `https://api.klingai.com/v1` | `kling-v1` |
| ComfyUI | `http://localhost:8188` | 本地部署 |

完整配置请参考 [.env.example](.env.example) 和 [DEPLOY.md](DEPLOY.md)。

## 📡 API 接口文档

### 基础信息

- **Base URL**: `http://localhost:8000`
- **Content-Type**: `application/json`
- **认证方式**: 部分接口需要 `user_id` 参数

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

### 对话接口

#### 同步对话

```
POST /chat
```

**请求体**：
```json
{
  "message": "我想给女朋友送花，预算200元左右",
  "user_id": "user_123456",
  "session_id": "optional-session-id",
  "location": {
    "lat": 39.9042,
    "lng": 116.4074
  },
  "shop_id": "shop_789"
}
```

**参数说明**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `message` | string | ✅ | 用户输入的消息 |
| `user_id` | string | ✅ | 用户唯一标识（由宿主平台传入） |
| `session_id` | string | ❌ | 会话 ID，不传则创建新会话 |
| `location` | object | ❌ | 用户位置 `{lat, lng}` |
| `shop_id` | string | ❌ | 店铺 ID，锁定后整个会话不变 |

**响应示例**：
```json
{
  "session_id": "conv_xxxx",
  "reply": "根据您的需求，我推荐以下方案...",
  "ui_cards": [
    {
      "type": "flower_recommendation",
      "data": { ... }
    }
  ],
  "thinking": "用户预算200元，需要浪漫风格...",
  "tools_used": ["knowledge_search", "generate_image"]
}
```

#### SSE 流式对话

```
POST /chat/stream
```

**请求体**：同同步对话接口

**响应格式**（SSE）：
```
event: thinking
data: {"content": "正在分析您的需求..."}

event: text
data: {"content": "根据您的需求，我推荐..."}

event: tool_call
data: {"tool": "generate_image", "status": "running"}

event: done
data: {"session_id": "conv_xxxx", "tools_used": ["knowledge_search"]}
```

**事件类型**：

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
  "status": "completed",
  "image_url": "https://...",
  "created_at": "2024-01-01T00:00:00"
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

## 📚 知识库管理

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
vim .env  # 填入生产配置

# 2. 构建并启动
docker-compose up -d --build

# 3. 设置开机自启
docker update --restart unless-stopped flora-agent

# 4. 查看状态
docker-compose ps
docker-compose logs -f agent
```

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
- `WX_APPID` 和 `WX_SECRET` 是否与小程序后台一致
- 小程序是否已发布或开启了开发版体验

### Q: 数据库连接失败？

**A**: 
- PostgreSQL 需要安装 `asyncpg`：`pip install asyncpg`
- SQLite 需要创建 `data/` 目录：`mkdir -p data`

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


