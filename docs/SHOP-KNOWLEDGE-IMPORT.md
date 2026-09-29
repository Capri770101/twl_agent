# 店铺知识资料准备规范（1.4.0 联调准备）

状态：资料模板与导入规范，不是已实现的导入服务。当前查询使用商家后端字段；独立知识索引/审核后台仍属后续建设。

## 每店资料清单

由门店确认营业与节假日安排、配送范围/费用/预约时限、定制及包装偏好、花材替换规则、取消政策、售后申请条件。没有资料的项目留空，不复制其他店的规则。

使用 `releases/v1.4.0/shop-knowledge-template.json`，每条只描述一个主题。店铺及平台编号必须由对接方提供真实映射；正文使用纯文本，禁止包含登录凭据、客户订单或私人地址。

## 字段约束

- platform_id + shop_id：归属，不能由模型推断；与业务后端编号一致。
- document_id + version：稳定文档ID和正整数版本；同版本不可覆盖，应创建新版本。
- topic：hours / delivery / customization / cancellation / after_sales。
- status：draft / published / withdrawn；模板默认 draft。
- content：门店确认的规则正文；空内容不得发布。
- source：来源名称、资料提供者角色；不能用模型生成文本冒充门店政策。
- effective_from / effective_to / updated_at：带时区 ISO 时间，未确认时为 null；到期规则不得继续用于承诺。

## 后续导入器验收要求

导入时校验店铺归属、主题白名单、版本及有效期；先预览校验结果再发布。检索先过滤平台、门店、published 与有效时间，再召回；不能仅在 prompt 中要求隔离。公共花艺知识单独存放，缺店铺规则时只提供通用建议，不保证可配送/可退款。

撤回后需清理检索索引与缓存；回答记录 document_id/version/source 以便追溯。实测覆盖 A店不能命中B店、过期/撤回排除、同店规则更新、缺资料时如实回答。
