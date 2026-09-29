# Flora Agent 1.4.0 候选范围

更新：2026-09-29。**已有本地实现，待商家联调，未正式发布**。源码基线 `58cf23c`；运行 VERSION 仍为 1.3.0，配套 H5 源码基线 `bedf2d7`、运行文件版本 1.0.5、目标 1.1.0。

## 已实现

- H5 有效登录换 5 分钟只读委托票据；agent `/auth/customer-token` 验签后签发独立 `customer_h5` 身份。
- `/chat` 与流式入口校验请求头业务凭据和当前身份绑定；凭据不作为模型参数。
- 固定 HTTPS 业务入口的公开店铺咨询、本人订单列表/详情及退款进度只读工具。
- `customer_shops`、`customer_orders`、`customer_login` 卡片与内容摘要；H5 登录问题回填、账号切换清理。
- `customer_service_logs`、`/api/metrics/customer-service` 和监控面板。指标是查询成功率，不是业务办理完成率。
- 店铺知识资料规范与 JSON 模板；没有实现独立知识库导入/审核/发布服务。

## 验证范围

- 本次 agent 全量 pytest 通过，详细结果见 [DELIVERY](../../DELIVERY.md)。
- 该提交历史记录包含 Node 签发/Python 验签互通、H5 构建和受控浏览器场景；本次文档整理未重跑 H5 或真实浏览器业务。
- 尚未完成真实数据库建表/聚合 SQL、商家订单/退款字段、真实模型、手机与跨账号完整验收。具体见 [联调清单](HANDOFF-CHECKLIST.md)。

## 开关与升级

agent/H5 服务端 `CUSTOMER_SERVICE_ENABLED=false`，H5 构建 `VITE_CUSTOMER_SERVICE_ENABLED=false`。只在隔离联调环境同时配置并启用；真实配置见 [配置清单](../../docs/CONFIGURATION.md)。

`init_db()` 增量创建客户服务监控表及索引，即使开关关闭也需要数据库权限。新客户身份与原 external_user_id 派生身份不同，旧历史不会自动迁移。

## 未交付能力

不创建/取消/修改订单，不办理退款，不提供人工转接或门店 DIY 报价确认；售后取自订单退款字段，不是完整多工单/部分退款流水系统。

## 正式发布待记录

最终 agent/H5 commit、版本、源码/镜像校验值、配置变更、备份、部署日期、真实验收报告均在正式发布时填写。本目录不是已发布升级包。

[实施计划](../../docs/CUSTOMER-SERVICE-V1.4-PLAN.md) · [升级说明](UPGRADE.md)
