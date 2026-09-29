# 配置与独立交接清单

更新：2026-09-29；依据 `backend/config.py`、数据网关、`docker-compose.yml` 和本地文件存在性核查。本文只记录变量名，不记录真实值。

## 1. 本地文件核查

| 文件/资产 | 本机状态 | GitHub / 交接方式 |
|---|---|---|
| `.env.example` | 已有，Git 跟踪 | 可随源码交；占位模板 |
| `.env.production` | 已有，Git 忽略 | 单独核对并安全交接；不是完整配置，且不会自动加载 |
| `.env` | 未发现 | 接收方从模板创建并填入真实值 |
| `.env.demo` | 未发现 | 需要演示栈时从 `deploy/env.demo.example` 创建 |
| `deploy/certs/` | 未发现 | 使用项目 nginx HTTPS 时单独提供证书和私钥 |
| `data/` | 已有，Git 忽略 | 运行文件，不应整体混入源码包；迁移时确认实际所需文件 |
| `data_demo/` | 未发现 | 演示运行后生成 |
| PostgreSQL 数据/备份 | 不在源码交付范围 | 需另向部署方取得备份或确认全新空库 |

Git 当前跟踪的环境文件只有 `.env.example`，未跟踪 `.pem/.key/.p12/.pfx` 证书文件。本次检查不是整个 Git 历史的密钥审计。

**发现的历史例外**：`docs/10-安全与合规.md` 曾直接写入一项历史监控面板密钥。本次已移除明文，但旧 Git 提交仍保留；请部署方轮换该历史 `DASHBOARD_API_KEY`，不要继续使用。本文和交接回复不复述密钥值。

**加载规则**：`main.py` 与 `Settings` 默认读取根 `.env`；Compose 主服务 `env_file` 也是 `.env`，演示为 `.env.demo`。仅使用 `--env-file .env.production` 影响 Compose 插值，不会自动改写服务的 `env_file` 路径。按模板准备实际 `.env` 最清晰。

## 2. 基础运行必需

| 配置 | 从谁取得/怎么准备 | 本地 `.env.production` 状态 |
|---|---|---|
| `APP_ENV`、`HOST`、`PORT` | 生产 `prod`；容器内部监听 8000 | 已设置，值未展示 |
| `DATABASE_URL` | 内部 PostgreSQL 连接串，不是商家库 | 已设置 |
| `POSTGRES_PASSWORD` | Compose 自带 PostgreSQL 密码；与连接串一致 | 已设置 |
| `DEMO_POSTGRES_PASSWORD` | 独立演示库密码；完整 Compose 插值需要此变量 | 未发现 |
| `JWT_SECRET` | 独立随机密钥，生产至少 32 字符 | 已设置；需部署方确认有效性 |
| `PLATFORM_API_KEYS` | `平台ID=密钥`，由智能体和接入方后端共同配置 | 已设置 |
| `AUTH_REQUIRED` | 生产为 true | 已设置 |
| `ANONYMOUS_LOGIN_ENABLED` | 正式主服务明确 false | 未发现显式配置 |
| `ALLOWED_ORIGINS` | 实际宿主页面 HTTPS 来源，逗号分隔 | 已设置；需核对当前域名 |
| `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` | 模型平台有效凭证、兼容接口地址、账号可用模型 | 已设置；本轮未请求验证 |
| `DASHBOARD_API_KEY` | 独立监控密钥 | **未发现**；不配监控接口返回 503 |

部署新环境可以生成新密钥；延续旧环境时，平台 ID 变化会影响用户身份，JWT 换密钥会使旧 token 失效。随机值由接收方在安全环境生成，不在仓库记录。

## 3. 真实效果图、贺卡和语音

| 功能 | 配置 | 注意 |
|---|---|---|
| 效果图/贺卡 AI 背景 | `IMAGE_PROVIDER`、`IMAGE_API_KEY`、`IMAGE_BASE_URL`、`IMAGE_MODEL` | 本地生产配置仅发现 `IMAGE_PROVIDER`，未发现独立图片 Key/地址/模型；不能据此保证真实出图 |
| 图片和音频 URL | `IMAGE_PUBLIC_BASE_URL` | 本地为空；留空使用 `/generated/...`，前端拼当前 API/代理入口 |
| 文件保存 | `DB_PATH` 的父目录下 `generated/`，Compose 挂载 `data/` | 当前实现写本地文件，不自动上传对象存储；CDN 要另配置回源 |
| 语音 | `SPEECH_ENABLED`、`SPEECH_API_KEY`、`SPEECH_TTS_MODEL`、`SPEECH_TTS_VOICE`、`SPEECH_ASR_MODEL` | Key 留空回退 `LLM_API_KEY`；主对话不是百炼时需单配百炼语音 Key |
| 中文字体 | `CARD_FONT_PATH`（可选） | 镜像自带文泉驿；本地 Python 要确认字体存在 |

本地 `.env.production` 没有显式语音变量；代码默认开启语音不代表凭证已有对应模型权限。手机浏览器录音需 HTTPS / 安全上下文。

## 4. 商品/店铺实时数据

选择一个经过授权的数据源：

- REST：`PLATFORM_API_<SOURCE_ID>_URL`、需要时的 `PLATFORM_API_<SOURCE_ID>_TOKEN`。当前适配器读取 `/v1/merchant/products` 与 `/v1/merchant/shops`。
- 数据库：`PLATFORM_DB_<SOURCE_ID>_URL`，只读账号、网络权限以及内部库中已激活的 `mapping_drafts` 映射。`data_mapping.json.example` 是示例，不等于已配置映射。
- 授权：`PLATFORM_SOURCE_ACCESS`，例如 `{"h5app":["aistore"]}`；平台 ID 必须对应 JWT/平台 Key，数据源 ID 必须对应变量名。客户服务身份需要通用商品源时另授权 `customer_h5`；匿名演示使用 `anonymous`。
- `PLATFORM_SINGLE_SOURCE_COMPAT=false`、`ENABLE_OPS_TOOLS=false` 保持默认；不要为省配置放开多租户边界。

上述数据源 URL/Token/连接串及授权变量在本地 `.env.production` **均未发现**。应向实际部署方取得当前可用源与授权配置，不能依赖旧文档中的历史公网地址。

## 5. 新客户服务：联调时才配置启用

| 所在端 | 配置/资料 |
|---|---|
| 智能体 | `CUSTOMER_SERVICE_ENABLED=false`；`CUSTOMER_SERVICE_BASE_URL=https://<可信业务入口>/api/customer-service`；`CUSTOMER_SERVICE_SECRET` |
| H5 后端 | `CUSTOMER_SERVICE_ENABLED=false`；同一个 `CUSTOMER_SERVICE_SECRET`；独立 `AUTH_SECRET` 和现有业务库/商家桥接配置 |
| H5 前端 | `VITE_CUSTOMER_SERVICE_ENABLED=false`；开启时需重新构建 |
| 联调资料 | 两个独立测试客户、专用订单及退款状态、店铺 ID 映射、可用 HTTPS、商家接口字段说明 |

专用签名密钥至少 32 字节，两后端一致，不与 `JWT_SECRET/AUTH_SECRET` 混用。普通平台换票不授予订单查询身份。开关开启后，H5 登录换取 5 分钟委托票据，再经 `/auth/customer-token` 换智能体身份；聊天请求头为 `X-Customer-Credential`。新身份不会自动继承旧对话。

本地 `.env.production` 未发现上述客户服务配置；默认关闭是预期。完整验收见 [HANDOFF-CHECKLIST](../releases/v1.4.0/HANDOFF-CHECKLIST.md)。

## 6. 按需配置

- 微信直登：`WECHAT_APPID/WECHAT_SECRET`（兼容 `WX_APPID/WX_SECRET`）；本地两项为空。宿主自己完成登录时不要求智能体直连微信。
- 成交学习回调：`LEARNING_WEBHOOK_SECRET`；未配置端点返回 503。
- Embedding：`EMBEDDING_PROVIDER/API_KEY/BASE_URL/MODEL/DIM`，默认关闭，开启前评测。
- Token 预算：`REDIS_URL` + `LLM_COST_ENABLED=true` + 正数预算；缺少任一项不实施预算限制。
- 腾讯模型备用：`HY_API_KEY` 与 `LLM_HY_FALLBACK_ENABLED`；默认不开启。
- 智谱视觉、地图：`ZHIPU_API_KEY`、`TENCENT_MAP_KEY`，按实际功能需要提供。
- `PLATFORM_ORDER_API_*`：不配置，智能体不直接下单。

## 7. 服务器、证书与数据迁移

- 服务器地址、SSH 用户/私钥单独管理，不随源码包分发。
- 使用内置 nginx：修改 `deploy/nginx.conf` 的实际域名，提供 `deploy/certs/fullchain.pem` 与 `privkey.pem`。历史域名和证书日期不能当当前可用证明。
- 内部 PostgreSQL 的 `pgdata`/`pgdata_demo` 卷不在 GitHub；保留历史会话、映射和任务需要单独数据库备份恢复。
- `data/generated/` 包含图片、贺卡和语音；数据库仅保存 URL，单搬数据库不能恢复文件。
- 启动 `init_db()` 会创建表/索引；新增监控表即便客户服务开关关闭也在 schema 中。受限数据库账号须由 DBA 按当前 schema 预部署，不能只执行旧 `001_image_tasks.sql` 就声称完整升级。

交接建议分成「源码包」「私密配置」「必要运行数据」三份，分别注明用途和接收人。私密值不进入 README、工单正文、Git 或 Docker 镜像。
