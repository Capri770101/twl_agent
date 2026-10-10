# 文档地图

当前工作入口：[智能体独立稳定性基线](AGENT-STABILITY-BASELINE.md)、[1.3.1候选日志](../releases/v1.3.1/RELEASE_NOTES.md)。客户服务/H5联调暂停。

更新：2026-10-11。交付版本以根 [DELIVERY](../DELIVERY.md) 为准，密钥/外部依赖看 [CONFIGURATION](CONFIGURATION.md)。

客户服务联调交接：[清单](../releases/v1.4.0/HANDOFF-CHECKLIST.md)；店铺资料：[准备规范](SHOP-KNOWLEDGE-IMPORT.md)、[JSON模板](../releases/v1.4.0/shop-knowledge-template.json)。v1.4 实施计划已归档：[archive/CUSTOMER-SERVICE-V1.4-PLAN](archive/CUSTOMER-SERVICE-V1.4-PLAN.md)。

版本范围：[RELEASE_NOTES](../releases/v1.4.0/RELEASE_NOTES.md)；更新文件与升级：[UPGRADE](../releases/v1.4.0/UPGRADE.md)。已有本地实现，未正式发布。

新增开发接口：[个性化贺卡 API](GREETING_API.md)、[语音 API（TTS/ASR）](SPEECH_API.md)。贺卡订单摘要由接入方提供；语音前端录音需 HTTPS（见 SPEECH_API.md）。

接收方 AI 先读根目录 `README.md` 和 `DELIVERY.md`。本文用于定位专题文档。

## 当前规范

正在推进的迭代：[可靠性实施与验收清单](RELIABILITY-PLAN.md)，包含已部署基线、未完成事项和每批文档要求。

本轮审查与交付边界见 [docs/archive/](archive/)（2026-09-24 联动审查等历史批次记录已归档）。

| 文档 | 用途 |
|---|---|
| `CONFIGURATION.md` | 密钥、配置、证书及数据独立交接清单 |
| `01-项目概述.md` | 产品与能力概览 |
| `03-系统架构设计.md` | 组件、数据流和架构决策 |
| `04-API接口文档.md` | HTTP API 与鉴权 |
| `05-前端对接契约.md` | SSE、卡片和任务协议 |
| `06-数据库设计.md` | 内部 PostgreSQL 数据模型 |
| `07-部署手册.md` | Docker Compose 部署 |
| `08-运维监控手册.md` | 健康检查、监控和排障 |
| `09-测试与验收.md` | 测试和验收 |
| `10-安全与合规.md` | 信任边界和安全要求 |
| `11-变更记录.md` | 历史变更与 ADR |
| `EVALUATION.md` | 真实模型场景评测规范 |
| `KNOWLEDGE_BASE.md` | 知识域、添加规则和质量门 |
| `IMAGE_STYLE_REFERENCE.md` | 店铺风格参考生图现状与下一阶段 |
| `GREETING_API.md` | 贺卡文案草稿与渲染接口 |
| `SPEECH_API.md` | 语音输入（ASR）与语音播报（TTS）接口 |
| `dev-and-release-workflow.md` | 开发、版本和发布流程 |
| `dependency-lock-linux.md` | Linux 依赖锁流程 |
| `retention-policy.md` | 数据保留策略 |

## 接入与业务专题

- [小程序接入待办清单.md](小程序接入待办清单.md) —— 宿主接入契约与验收项；与 `05-前端对接契约` 冲突时**以该清单为准**
- [INTEGRATION-v1.4.0.md](INTEGRATION-v1.4.0.md) —— 1.4.x 各组件接入总说明
- `DIY成交通道方案.md`（讨论稿）已归档至 [archive/](archive/)，当前不代表已接真实 DIY 订单

## 发布与历史记录

一次性发布记录（`release-*.md`、`session-memory-release.md`、`platform-access-release.md`）、审查报告（`REVIEW-20260924.md`、`code-review-修复报告.md`）、历史规划（`OPTIMIZATION_PLAN.md`、`优化路线图.md`）以及 2026-10-11 本批归档（PRD、架构方案稿、客服交接快照、小程序交接单等 14 份）均在 [docs/archive/](archive/)，通过 Git 历史可追溯，不是当前部署手册。当前状态以根目录 `DELIVERY.md`、`CHANGELOG.md` 和源码为准。

已被替代的旧部署/接入说明和早期贺卡记录的清理说明见 `archive/README.md`。当前待验收范围看 DELIVERY、RELIABILITY-PLAN 和源码。

## 维护规则

- API、环境变量、版本、测试数和开关变化时同步根 README、DELIVERY 和 CHANGELOG。
- 一次性发布记录不复制到多个长期文档；最终结论只保留在当前规范中。
- 每个主题只保留一份"当前有效"文档；被替代或带日期快照的文档移入 `archive/`，不物理删除。
- 文档示例不得包含真实密钥、令牌、密码、用户 ID 或证书内容。
