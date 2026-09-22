# Flora Agent 交付说明

> 面向接收方 AI、研发和运维。当前有效结论以本文、README、源码和最新 CHANGELOG 为准；`docs/archive/` 仅供追溯。

## 当前版本

| 项目 | 值 |
|---|---|
| 智能体版本 | `1.2.0` |
| 当前源码修订 | 使用 `git rev-parse HEAD` 获取；不在文档写死当前 HEAD |
| 功能基线提交 | `3feadb2` |
| 评测与路由提交 | `779ada3` |
| 全量测试 | `784 passed`（2026-09-22，本地） |
| 原生产 API | `api.tiaowulan.com` 已停用，不作为当前验收入口 |
| 开发验收 | 本机 API / 容器内网；连接地址由部署方配置 |

配置默认值（域名停用后未重新核验运行服务器状态）：

```dotenv
CARE_TOOL_SCOPE_ENABLED=false
AGENT_INTENT_ROUTING_ENABLED=false
```

历史演示曾开启这两个开关做单次样本对比；该记录不是当前部署状态或性能保证。

## 系统边界

- 智能体是纯后端 HTTP 服务，不是小程序或 H5 前端。
- 商品、店铺数据只读查询；订单和用户实体默认拒绝。
- DIY 方案是估算方案，不是平台 SKU；当前不创建真实订单、不收款、不分账。
- 生图为异步任务：响应返回 `task_id` / `poll`，客户端轮询 `GET /tasks/{task_id}`。
- 个性化贺卡开发接口：`POST /greetings/draft` 生成可编辑文案，`POST /greetings/render` 生成 PNG；当前订单摘要由宿主提供，接口尚未绑定真实订单事件。
- 前端只能渲染后端结构化 `diy=true` 的方案，不得从自然语言猜 DIY 卡。

## 关键目录

```text
agent/agent.py                 ReAct 主循环、护栏、UI 推导
agent/engine/intent.py         候选意图路由
agent/engine/tool_scope.py     请求级工具范围
agent/plan_validator.py        DIY 方案确定性校验
agent/toolkit.py               工具注册与执行
backend/routers/chat.py        /chat 与 /chat/stream
backend/data_gateway/          数据源授权和只读查询
backend/storage/               PostgreSQL 会话、任务和记忆
evals/flower_scenarios.jsonl   真实场景评测集
scripts/validate_eval_set.py   评测集校验
tests/                         pytest 回归测试
```

## 本地验证

```bash
python -m pytest -q
python scripts/validate_eval_set.py
python scripts/validate_knowledge.py
git diff --check
docker compose build agent agent-demo
```

## 生产配置重点

生产 `.env` 只存在服务器，不进仓库。核对 `DATABASE_URL`、`JWT_SECRET`、`PLATFORM_API_KEYS`、`PLATFORM_SOURCE_ACCESS`、`AUTH_REQUIRED=true`、`ANONYMOUS_LOGIN_ENABLED=false`、`ENABLE_OPS_TOOLS=false`。不要提交 `.env`、API Key、JWT、数据库密码或证书私钥。

## 无公网域名时的开发验收

`api.tiaowulan.com` 下线不影响本地或内网开发：

```bash
docker compose up -d postgres agent
curl http://127.0.0.1:8000/health

docker compose --profile demo up -d --build
curl http://127.0.0.1:8010/health
```

本地接入方将 API base URL 指向 `http://127.0.0.1:8000`；容器内接入使用 `http://agent:8000`。贺卡图片返回 `/generated/...png`，本机可通过对应端口的 `/generated/` 路径验收。没有平台数据源时仍可验收知识库、DIY 结构和贺卡渲染，但不能验收实时商品推荐。

## 发布与回滚

代码必须先提交并更新 `CHANGELOG.md`，通过测试和 Docker 构建；先演示、后生产。发布前备份数据库、旧代码和镜像，记录 commit、开关和发布目录。回滚优先恢复旧镜像或关闭 feature flag，不删除数据库字段和运行数据。

H5 是独立仓库，当前版本和发布入口见 `D:\Work\tiaowulan\H5\README.md`、`CHANGELOG.md` 与 `docs/RELEASE_PROCESS.md`。
