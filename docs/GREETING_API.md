# 个性化贺卡 API（未发布）

本地地址由运行配置确定，不依赖已停用的 api.tiaowulan.com。
接口使用现有 Bearer JWT，user_id 必须与 token sub 一致。AUTH_REQUIRED=false 仅限本地开发。

## 1. 生成可编辑草稿

`POST /greetings/draft`

```json
{"user_id":"换票返回的用户ID","items":[{"name":"白玫瑰花束","quantity":1}],"recipient":"女朋友","occasion":"生日","customer_intent":"感谢这一年的陪伴，希望她开心自在","tone":"warm"}
```

返回 text、needs_confirmation=true、context_source=caller_supplied。
items 是调用方提交的商品摘要，不是智能体验证过的订单；订单归属校验由宿主后端在提取摘要前完成。
禁止传手机号、地址、支付数据等完整订单字段，接口拒绝额外字段。不需要提供订单号。
tone 支持 warm/literary/playful/formal/deep。

## 2. 确认或修改后渲染

`POST /greetings/render`

```json
{"user_id":"换票返回的用户ID","text":"用户确认后的正文","recipient":"亲爱的你","sender":"爱你的我","occasion":"生日","template":"blush"}
```

返回 `{ui:"greeting_card",data:{image_url,text,recipient,sender,template,...},ai_generated:true}`。
正文最多200字，template 支持 warm/blush/green/letter/night。
此接口直接渲染提供的文字，不再调用文案模型。图片是本地PNG，返回相对地址时拼接当前API地址。

## 错误与限制

403 身份不一致；422 参数不合规；429 限流；503 当前实例繁忙；504 超时；502 模型或渲染失败。
使用进程内2个并发槽位和现有按用户限流；多进程不共享限流。
没有新增数据库表，不自动绑定订单，不自动通知客户，不自动触发支付或订单完成事件。
测试中的模型返回使用替身，真实模型文案质量仍需使用部署方凭证验收。
