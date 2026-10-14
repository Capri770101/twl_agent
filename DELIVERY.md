# 花艺智能体交付说明

更新：2026-10-14。本文件描述 **1.5.0 源码候选，尚未在服务器部署**；1.4.x 从未上线，本批完成 v1 编排退役（v2 脊柱成为唯一路径）与交易闭环的配置门控准备。具体请求、权限、UI/SSE、通知与各方责任见 [1.4.x 对接说明](docs/INTEGRATION-v1.4.0.md)；文档示例不等于接口实测。

## 1. 交付定位

- 版本号：`VERSION=1.5.0`。准确源码由 Git 提交定位，不能只依靠版本号；现网 1.4.0 未经近两轮更新。
- 编排：v2 脊柱（`agent/spine.py`）是唯一编排路径。v1 关键词主循环与 `AGENT_ORCHESTRATION`/`AGENT_INTENT_ROUTING_ENABLED`/`CARE_TOOL_SCOPE_ENABLED` 开关已删除；回退手段是 Git revert 到 1.4.1（`b2a691d`），不是配置切换。
- 本仓库只交付智能体源码、schema 定义及接口；启动默认执行完整 schema DDL，现有 `001_image_tasks.sql` 仅含图任务表，不足以作为 DBA 限权部署的完整迁移。不包含 H5 浏览器 JWT、真实交易写端点、商家 IM、外部推送通道或服务器密钥。
- `create_order`（代客下单）能力已内置并恢复注册，但默认 `PLATFORM_ORDER_ENABLED=false`（fail-closed）：开关与 `PLATFORM_ORDER_API_URL` 同时配置后才对模型暴露、执行层才放行；未接通时模型如实引导用户去小程序结算页，绝不代客下单。平台下单 API 的字段对齐点在 `agent/skills/skill_order.py::_build_payload/_parse_platform_order`。
- `cancel_my_order`、`apply_refund`、`update_order_address` 的能力与确认流程已内置，但默认 `CUSTOMER_SERVICE_WRITE_ENABLED=false`（fail-closed）：未配置写通道时不对模型暴露、执行层拒绝调用，模型如实引导用户去平台订单页。接入真实写端点后置 true，并同步扩展 cs1 票据的 write scope（当前仅 orders:read）。
- `contact_merchant` 仅在智能体本地留痕，`merchant_notified=false`；带订单号的工单须用 cs1 票据查到本人订单，否则拒绝关联。没有商家主动通知承诺。
- 提醒按北京时间解释、以 UTC 入库存储；到点只写 `/api/notifications` 收件箱。接入方须自己消费并展示/推送，未接入前不能保证消费者收到通知。
- 流式接口会流式读取模型和执行工具，但只有验证、清理后的最终文字才发给客户端；不再逐字显示未经证实的付款、工单投递和出图承诺。接入方须接受首字延迟可能上升，并以最终 `text`/`done` 事件为准。新增 `status` 阶段事件（`phase: understand/react`）供前端渲染「正在理解/处理」状态，纯增量、可忽略。
- 真实 H5/小程序联调、图片/语音供应商、生产数据库迁移与负载验收属于独立发布门槛。**未完成上述集成不得称完整交易版已上线。**

## 2. 交付内容

- 源码、知识库、提示词、测试、依赖、Docker/Compose、配置模板及当前文档。
- 已有能力：问答、商品/店铺查询、DIY 修改和恢复、版本历史、异步效果图和贺卡、ASR/TTS、监控。
- 当前增量：本人订单/售后只读查询、店铺评价、本人工单列表与详情、提醒站内收件箱与规则查询/暂停/恢复/修改/软删；这些能力仍需 H5 真实票据、数据源和前端页面接入。
- H5 是独立项目；此处不提供用户 JWT 写权限或商家消息消费者。

源码包不要直接压缩整个工作目录。使用最终提交的 Git 归档，避免带入 `.env.production`、`.git/`、缓存和用户数据；归档只包含已提交的文件。

## 3. 需要单独交接的东西

完整变量表、现有文件核查和缺项见 **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**。

1. 真实运行配置：数据库、模型 API Key、JWT、平台 Key、监控 Key、数据源授权。
2. 客户服务启用时：与 H5 一致的专用签名密钥、可信 HTTPS 业务入口、两套测试客户及订单。
3. 基础设施：实际域名、TLS 证书与私钥、服务器访问方式、数据库访问权限。
4. 延续旧环境时：数据库备份、有效数据源映射、生成图片/音频、必要的运行目录。新装空环境与迁移旧历史是两种交付范围，需明确。

本地 `.env.production` 在 Git 之外，但仅是部分配置；程序不自动加载该文件。不能声称“生产配置只存在服务器”或“拉 GitHub 即能原样运行”。

## 4. 本次验证记录

当前 1.5.0 候选在本机 PostgreSQL 测试库（`127.0.0.1:5433/flora_agent_test`）运行 `python -X utf8 -m pytest tests/ -q`（另设 `FLORA_TEST_DATABASE_URL` / `REMINDER_TEST_DATABASE_URL` 指向同库验证跨连接路径与 `SKIP LOCKED`）：**1188 项收集、1187 通过、0 失败、0 ERROR、1 跳过**。唯一跳过项是 Node/H5 签发凭据跨语言互通测试 `test_node_issued_credential_accepted_by_python`（须显式 `RUN_H5_INTEROP=1` + 相邻 H5 仓库及 Node，非本机范围）。

同机复核：`ruff check .` 全仓通过；CI mypy 阻塞门（domain + 抽取模块，21 文件）通过；`validate_eval_set.py` 39 场景、`validate_knowledge.py` 7 知识域通过。下单写通道离线契约由 `tests/test_order_channel_e2e.py`（6 用例，mock 端点 + 真实工具层）覆盖。

**尚未完成、发布前必须补的验证**（见 §5）：39 场景**真实模型**评测在 v1 退役后未重跑（本机无可用 `LLM_API_KEY`；离线 e2e 已覆盖 `_finalize_turn` 判据变更的确定性回归，但真实模型行为需在有 Key 的环境确认）；下单通道未经真实平台端点联调；本版未生产部署或真实手机验收。

历史记录（1.4.1 候选，2026-10-13，隔离测试库）：976 项收集、975 通过、0 失败、1 跳过；39 场景 v1/v2 对照评测无回归。

历史记录（2026-09-29，基线 `58cf23c`）：

- `python -m pytest -q`：873 项全部通过。
- `python scripts/validate_eval_set.py`：8 个场景结构有效。
- `python scripts/validate_knowledge.py`：8 个知识域校验通过。
- Docker：本机没有 Docker CLI，本次未验证镜像构建/容器启动。
- 真实业务/模型/数据库/生产服务器：本轮未执行验收。

测试使用隔离数据与替身，通过不代表真实数据库迁移、自然语言所有场景或手机录音均通过。

## 5. 尚需完成的验收

- PostgreSQL 初始化及升级：包括 `speech_logs`、`customer_service_logs`；验证方案事务锁与并发生图复用。
- 模型供应商可用额度、实际模型 ID、效果图/贺卡视觉、ASR/TTS 实测。
- 客户服务可信 HTTPS、H5 登录签名与短票据、A 用户不能查 B 用户订单、账号切换、订单/退款状态核对。
- **下单写通道（1.5.0 新增，代码侧已离线契约验证）**：平台方交付真实下单 API 文档 → 对齐 `agent/skills/skill_order.py` 的 `_build_payload`/`_parse_platform_order` 字段与**金额单位（分 vs 元）** → 配 `PLATFORM_ORDER_ENABLED=true` + `PLATFORM_ORDER_API_URL`（+可选 KEY）→ 真机走通「推荐→确认→下单→支付跳转→平台侧订单可见」全链路。离线验证基线：`tests/test_order_channel_e2e.py`（mock 端点 `scripts/mock_platform_order.py`，含幂等/鉴权/故障注入）。
- 真实前端与手机：流式、版本恢复、重试、图片任务、退出登录、录音权限；新增 `status` SSE 事件的渲染适配（可选，未适配自动忽略）。
- 39 场景真实模型评测在 v1 退役后重跑（`scripts/run_eval.py`）：`_finalize_turn` 兜底判据已从关键词改为工具证据，需真实模型确认无行为回归。
- 店铺独立知识库尚未实现导入/发布服务；当前模板不是运行功能。

## 6. 发布和回滚

按 [发布流程](docs/dev-and-release-workflow.md) 记录最终 commit、镜像 ID/摘要、源码包 SHA-256、配置项名称、备份和验收结果。先联调，后正式发布。客户服务未验收前保持 agent/H5 服务端和 H5 构建开关关闭。

回滚先关闭新能力再还原应用；新增监控表和运行数据保留。不要因应用回滚而盲目覆盖整库。部署步骤见 [部署手册](docs/07-部署手册.md)。
