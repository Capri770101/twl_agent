# 智能体评测规范

## 目的

pytest 验证程序契约；场景评测验证真实模型的工具选择、结构化输出、业务事实和性能。
两者都通过后才允许把 feature flag 从演示环境推进到生产环境。

## 场景集

`evals/flower_scenarios.jsonl` 是当前基线，覆盖养护、商品、DIY、生图、贺卡和混合需求。
每条场景定义意图、允许/禁止工具、最大轮数、UI 和业务检查项。

离线校验：

```bash
python scripts/validate_eval_set.py
```

## 真实评测记录

每次模型、prompt、工具范围或业务护栏变化，都记录：

- git commit / 版本号
- 模型与 provider
- 场景 id
- 最终 UI、工具序列和检查结果
- prompt/completion tokens
- 总耗时、LLM 轮数和重试次数
- 失败样本与回归结论

生产阈值建议：独立 QA 误调用业务工具为 0；生图请求无真实方案时不得生成任务；商品价格、方案卡和文字不得互相矛盾。
