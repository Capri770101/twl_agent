# 文档地图

新增开发接口：[个性化贺卡 API](GREETING_API.md)，当前未部署，订单摘要由接入方提供。

接收方 AI 先读根目录 `README.md` 和 `DELIVERY.md`。本文用于定位专题文档。

## 当前规范

| 文档 | 用途 |
|---|---|
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
| `dev-and-release-workflow.md` | 开发、版本和发布流程 |

## 接入与业务专题

- `接入方调用示例.md`
- `小程序AI页-前端改造交接单.md`
- `小程序出图对接说明.md`
- `小程序接入待办清单.md`
- `DIY成交通道方案.md`：讨论稿，当前不代表已接真实 DIY 订单。
- `知识库概览.md`、`知识库资料汇编.md`

## 发布与历史记录

`release-*.md`、`session-memory-release.md`、`platform-access-release.md` 和 `code-review-修复报告.md` 是特定批次记录，不是当前部署手册。当前状态以根目录 `DELIVERY.md`、`CHANGELOG.md` 和源码为准。

`archive/` 中全部文档仅供追溯，可能包含已经淘汰的模型、数据源和部署说明。

## 维护规则

- API、环境变量、版本、测试数和开关变化时同步根 README、DELIVERY 和 CHANGELOG。
- 一次性发布记录不复制到多个长期文档；最终结论只保留在当前规范中。
- 文档示例不得包含真实密钥、令牌、密码、用户 ID 或证书内容。
