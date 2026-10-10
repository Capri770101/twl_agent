> ⚠️ **本文档已归档**，仅作历史参考，不代表当前实现。
> 现行文档见 [docs/README.md](../README.md)；当前状态以根 README、DELIVERY.md 和源码为准。

# 第二阶段候选方案：图编排、事件记忆与增量文本

> 状态：设计评估，不是上线/迁移许可。基线：本地 `9c9216d`；真实 HTTP/模型评测未运行，不能把静态场景测试视作产品质量证明。

## 1. LangGraph 是否替换

**目前不替换。** 现有 `agent/orchestration/executor.py` 已抽出通用轮次与工具批次，调用方保留会话事实、护栏、存储和输出。当前对话是有上限的同步 ReAct 回合，尚无可证明需要跨进程持久化暂停/恢复或人工审批的流程；引入图运行时不能取代 `agent/toolkit.py` 的工具授权、`backend/storage/memory.py` 的用户归属和 `agent/agent.py` 的事实/卡片守卫。

若出现实际的人工审核、持久化等待或多分支恢复需求，做**隔离原型**而非全面替换：以 `(user_id, session_id, run_id)` 绑定 checkpoint，拒绝跨用户读；工具副作用使用稳定 `(run_id, node_id, call_id)` 幂等键；恢复时重新检查当前店铺锁、权限和客户凭证，不能信任旧 checkpoint；取消后禁止继续写状态或释放未经验证的 SSE。对照同一评测场景，比较 p95 延迟、调用数、失败恢复及真实平台副作用。达不到隔离/幂等/兼容门禁则停留在现有 executor。

## 2. 事件记忆：先影子投影，绝不直接替换历史

现状：`messages(id BIGSERIAL, session_id, role, content, ui, data, created_at)` 是有序展示/历史来源；`sessions` 绑定用户和店铺。`save_messages` 批量单事务插入，`load_history` 逆序取最新 user/assistant，`search_user_history` 通过 sessions 做用户归属 JOIN。`delete_conversation` 删除消息和会话。长期偏好在 `user_preferences`；临时槽位/会话数据还在 sessions 和 memories。现有记录**没有请求幂等键或事件版本**，不能直接声称可无损导出“用户改口”事件。

建议设计，不立即建表：

1. 定义只增的 `memory_events`：`event_id`、`user_id`、`session_id`、`source_message_id`（可空）、`source_revision`、`event_type`、`payload`、`observed_at`、`schema_version`、`dedupe_key`，唯一约束 `(user_id, dedupe_key)`。只有经过身份校验的用户陈述和经工具证实的事实可进投影；模型推测/原始工具 JSON 不得提升为事实。
2. 以 `messages.id` 为旧数据回填水位，按 `(user_id, session_id)` 扫描，事务内写事件及投影检查点；在线双写须同一个事务或可靠 outbox，不能先写事件后漏写消息。未知源标记为 `legacy_unverified`，不自动成为长期偏好。保持原 messages 的读取和展示为权威，影子投影对比输出，不影响线上对话。
3. 投影含版本和源水位；重建只消费当前用户事件，按 `(source_message_id, source_revision, event_id)` 稳定排序，重跑相同事件必须得到同一结果。显式 correction/retraction 覆盖旧槽位，用户最新改口优先；用户删除时在同一归属事务删除事件、投影、摘要和重建材料，离线备份/留存另须产品与合规决定，不能假装删除在线行就覆盖备份。
4. 检验重复提交、事务回滚、乱序回填、并发重建、跨用户查询、会话删除、回滚到旧读取路径。通过评测闭环和历史投影对比后才讨论切换读取；保留旧表及停用双写的回退窗口。

滚动摘要只消费明确闭合的 user/assistant 轮次，保存 `(user_id, session_id, source_max_message_id, summary_version)`；若该水位以内发生修订/删除，摘要失效并从源重建。临时预算、尚未确认的建议、订单事实与长期偏好分开；不能把摘要当授权或平台事实来源。

## 3. 真正增量输出：先证明可公开，再发字节

`ReActAgent._stream_llm()` 可以产生 `text_delta/text_rollback`，但 `run()` 的 `_model_message` 过滤它们；当前 `/chat/stream` 发送 `tool_call`、最终清理后的 `text`/`card`/`done`。`done.reply` 是最终权威。原 `arun_stream` 注释仍描述“逐字推送”和“原始输出”，属于过时契约描述，不能当作现状。即使前端支持回滚，用户已看过错误的价格、店铺、下单或“已出图”声明，回滚无法撤销泄露。

可选分级：先流式发送**非内容**进度（工具开始/完成、等待），保留最终校验后整段文本；若要流式正文，只允许独立、已验证的数据模板片段，先确定结构化工具证据和引用，后生成该片段，校验脱敏/XSS/卡片出处再发布。任何尚需后续 guard 才能判真的 LLM 原始文本不得发 delta；异常、取消、超时时只输出中性错误且不写未经校验的最终消息。对比流式与非流式的同一最终 reply/ui/data，测试零工具谎报、混合工具、隐私脱敏、取消、重复订阅和失败重试。没有客户端“只追加已验证片段”的协议和真实场景评测之前，不开启正文增量。

## 决策输入（需用户/产品提供）

- 真实行为评测的**隔离** HTTP 服务地址、测试身份/店铺/平台替身、模型费用与延迟上限；禁止默认为生产。
- 是否真有跨进程长等待/人工审核；若无，LangGraph 原型不优先。
- 消息和偏好保留期限、删除范围（含备份）、用户纠错含义；未定前不实施事件表或自动长期摘要。
- 接入端能否展示可信片段/证据及接受仅进度事件；未确认前沿用最终整段 SSE。
