# 贺卡本地验收与实现边界

## 已实现

`current_greeting_context` 读取当前用户、会话保存的 selected_plan，缺少时读取 latest_diy_plan。
`suggest_greetings` 仍是预设候选匹配，会用方案补充缺失的收卡人、场合和风格。
`render_greeting_card` 接收调用方提供的正文，使用 Pillow 五套模板生成图片。
正文若由主对话模型撰写，属于现有 ReAct 流程，并没有新增独立的订单文案生成接口。

## 默认回归门

```bash
python -m pytest -q tests/test_greeting_context.py tests/test_greeting_render_e2e.py tests/test_knowledge_quality_gate.py
python scripts/validate_knowledge.py
```

渲染测试将输出写入 pytest 临时目录，不写生产数据目录：

- warm、blush、green、letter、night 全部真实渲染。
- 校验 PNG 格式、完整解码、正文返回与方案上下文。
- 本机随机端口提供临时静态 HTTP 服务，验证返回字节与生成文件相同。
- 这里不是生产 FastAPI 路由集成测试，也不是字体视觉验收。

## 新增接口与剩余接入

已提供 `/greetings/draft` 和 `/greetings/render`，详见 [接口说明](GREETING_API.md)。以下订单授权、前端确认和业务触发仍需宿主集成；独立文案接口已完成本地模拟模型测试，真实模型质量待验收。

1. 宿主后端提供已授权的订单摘要，仅传商品/关系/场合等必要事实。
2. 客户表达作为独立字段，而不是把方案已有 card_message 当作客户原话。
3. 生成文案、用户修改/确认、最终渲染组成清晰接口；生成失败明确提示。
4. 中文字体缺失、长文本裁切、节日/慰问场景的文案质量与视觉验收。

域名已停用，本批不执行公网验收或部署；图片相对 URL 需要拼接当前开发 API 地址。
