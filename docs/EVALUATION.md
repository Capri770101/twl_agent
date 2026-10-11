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

真实 HTTP 评测（报告默认写入已忽略的 `evals/results/`）：

```bash
# H5 同源代理注入平台 Key
python scripts/run_eval.py --base-url http://129.204.85.139/agent

# 直连智能体：由进程环境提供 Key，不要写进命令或报告
EVAL_PLATFORM_API_KEY=... python scripts/run_eval.py --base-url http://127.0.0.1:8000
```

默认即使有失败也输出完整报告并返回 0；发布质量门使用 `--strict`。机器判定覆盖 UI、工具范围及结构化 `checks`。字符串形式的自然语言检查仍需人工复核，报告列出 `manual_check_items` 并标记 `needs_review`，**不计入 passed**；`--strict` 遇到失败或待复核均返回非零。`machine_passed` 只表示自动检查通过，不代表回答质量已经验收。需要发布前逐条复核报告中的人工检查项并记录结论；若能转为可验证的结构化断言，可消除待复核状态。

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
