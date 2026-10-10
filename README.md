# 花艺智能体

纯后端 AI 服务：提供花艺问答、商品与店铺只读查询、多轮 DIY 方案、异步效果图、贺卡、语音及调用监控，供 H5 / 小程序 / App 的宿主后端接入。

## 当前版本与交付状态

核查基线：**v1.4.0 源码交付候选**。以交付清单中的 Git 提交、SHA-256、测试结果为准；不表示 H5 写接口或线上部署已完成。

| 项目 | 当前边界 |
|---|---|
| 运行版本 | `VERSION` 为 **1.4.0**；不应仅凭版本号判断线上部署代码 |
| 自动验证 | 在隔离测试库执行 `python -X utf8 -m pytest tests/ -q`；实时结果记录在 [交付说明](DELIVERY.md) |
| 客户服务 | 本人订单/售后只读；取消订单、退款申请、改地址均返回 `CHANNEL_UNAVAILABLE`，不伪称成功 |
| 商家联系 | 工单仅写本服务数据库，**未投递商家**；如查到真实门店电话可供用户自行联系 |
| 提醒 | 北京时间登记，UTC 持久化；到点写站内通知收件箱，**尚无主动外部推送** |
| 线上状态 | 本次交付不代表生产部署；H5 接口和商家通道需接入方独立验收 |

最新变更见 [CHANGELOG.md](CHANGELOG.md)。

## 交接先看这三份

1. [DELIVERY.md](DELIVERY.md)：交哪一版、交付范围、验证结果和未完成事项。
2. [配置与独立交接清单](docs/CONFIGURATION.md)：密钥、数据库、域名、证书、数据源、H5 配置。
3. [部署手册](docs/07-部署手册.md)：按当前 Compose 启动、更新、回滚。

完整专题索引见 [docs/README.md](docs/README.md)。已被替代的旧说明从当前目录移除，可通过 Git 历史追溯。

## 能力与边界

| 能力 | 当前源码行为 |
|---|---|
| 对话与知识检索 | `/chat`、`/chat/stream`；平台/用户隔离、历史会话、画像和花艺知识库 |
| DIY | 创建、局部修改、约束检查、版本快照及恢复；`GET /conversations/{id}/plans` |
| 效果图 | 绑定真实方案，异步返回 `task_id/poll`，轮询终态 `done/failed/not_found` |
| 贺卡 | `/greetings/draft` 文案草稿；`/greetings/render` 异步 AI 背景 + 服务端中文排版 |
| 语音 | `/speech/transcribe`、`/speech/tts`；对话返回内容摘要 `speech_text`，前端录音需要安全上下文 |
| 商品与店铺 | 通用数据网关只读查询；需要真实数据源及 `PLATFORM_SOURCE_ACCESS` 授权 |
| 客户服务 | 公开店铺咨询、本人的订单/售后进度只读、评价查询、本地联系单；取消、退款、地址修改通道尚未打通 |
| 提醒 | 规则登记、北京时间调度、站内通知收件箱；外部推送需要接入方消费 API |
| 监控 | `/api/metrics/*`，含对话、工具、语音、图片任务及客户服务指标 |

智能体不创建真实订单、不收款、不办理退款、不提供已接通的人工转接。通用网关对 `order/user` 仍拒绝；客户服务通过独立 H5 业务后端按用户授权查询，不能把两者混为一条开放数据通路。店铺独立知识库目前只有规范和模板，没有导入服务。

## 快速开始

### Docker（推荐交接方式）

先准备 Docker Compose，以及 `.env`、独立的 `.env.demo` 模板配置。程序默认读取 **`.env`**；`.env.production` 不会被自动加载。具体填写项见 [配置清单](docs/CONFIGURATION.md)。

```bash
cp .env.example .env
cp deploy/env.demo.example .env.demo
# 填写数据库密码、JWT、平台密钥、模型和允许的来源；两套配置按模板独立填写。
docker compose config --quiet
docker compose up -d --build postgres agent
curl http://127.0.0.1:8000/health
```

默认只启 `postgres` 和 `agent`。`nginx` profile 增加 nginx/dashboard；`demo` profile 增加独立演示库和 agent-demo。启用 nginx 前配置实际域名与证书。

### 本地 Python

```bash
python -m venv .venv
# 激活：Windows PowerShell 用 .\.venv\Scripts\Activate.ps1；Linux 用 source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
# 准备 .env，DATABASE_URL 指向本机能连接的 PostgreSQL。
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Compose 的 PostgreSQL 默认不映射宿主机端口；本地 Python 不能直接使用容器 DNS `postgres`。如需这种组合，单独配置本机数据库或仅绑定 loopback 的数据库端口。启动时 `init_db()` 建表/索引，数据库账号需相应权限。

### 接入流程

宿主后端验证自己的用户 → 服务端携带 `X-API-Key` 调 `/auth/token` → 使用返回的 JWT 和 `user_id` 调 `/chat` 或 `/chat/stream`。平台密钥不进入前端；后续对话回传 `session_id`。

客户服务使用另一套短期委托票据及 `/auth/customer-token`，见 [联调清单](releases/v1.4.0/HANDOFF-CHECKLIST.md)。

## 验证

```bash
python -m pytest
python scripts/validate_eval_set.py
python scripts/validate_knowledge.py
git diff --check
```

单测通过不代替真实 PostgreSQL、模型供应商、商家接口和手机浏览器验收。真实模型评测方法见 [EVALUATION.md](docs/EVALUATION.md)。

## 目录

```text
agent/          ReAct、工具、提示词、花艺知识、方案和图片逻辑
backend/        API、鉴权、数据网关、存储、语音和监控
dashboard/      调用监控静态站点
demo/           演示页面
tests/          自动回归测试
evals/          场景评测集
scripts/        验证和运维脚本
migrations/     已提供的数据库迁移
deploy/         nginx 与演示配置模板（真实证书单独提供）
releases/       下一版本联调与升级资料，不代表已正式发布
docs/           当前专题说明及明确标记的历史记录
```

## 配置与维护原则

- `.env.example` 只放占位值；真实 `.env*`、证书、数据库备份和生成文件单独交接。
- `data/generated/` 是本地持久文件目录；`IMAGE_PUBLIC_BASE_URL` 只改 URL 前缀，**不会自动上传 OSS/CDN**。
- 改代码需要重新构建镜像；改 `.env` 需要重建容器环境，单纯 `restart` 不会读取新变量。
- 不写死测试数量、当前服务器地址或已发布状态到多处文档；验收记录必须注明时间、源码提交和范围。

## 版权与许可

仅供合作方内部使用，合同授权范围外禁止外发、二次分发或反向工程。
