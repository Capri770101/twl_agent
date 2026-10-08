# 智能体候选发布：独立养护问答工具范围

## 变更

- 新增 `CARE_TOOL_SCOPE_ENABLED` feature flag，默认 `false`。
- 开启后，明确的独立养护问题只向模型暴露 `retrieve_knowledge`、`respond_to_user`、`search_history`、`get_user_profile`。
- 混合购买、预算、方案、店铺、生图、贺卡和记忆请求继续使用完整工具集。
- 执行层再次拒绝本轮不允许的工具，避免模型幻觉调用。

## 验证

- 本地全量 pytest：759 项通过。
- 新增工具范围与并发上下文测试：13 项通过。
- 生产尚未开启开关；须先在演示实例做同一问题的 token、延迟、回答质量对比。

## 演示验收问题

```text
玫瑰怎么养得久？只讲养护，不要方案和图片。
```

对比指标：`prompt_tokens`、`completion_tokens`、总耗时、是否调用非知识工具、回答是否完整。

若失败，关闭 `CARE_TOOL_SCOPE_ENABLED` 即可回到原行为；若通过，再在生产启用并记录发布版本。
