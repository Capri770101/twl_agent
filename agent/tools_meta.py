"""工具与 UI 的**元数据单一来源**（2026-09-20 集中化，外部 code review 第 10、11 条）。

## 为什么单独一个文件

此前这些分类散在 `agent/agent.py` 各处：`_CARD_UIS` 在 270 行、三个工具元组在 699+ 行、
UI→Action 映射则直接写在 `run()` 里。结果是「新增一个工具 / 一种 UI」要改 5~6 个地方，
而且**没有任何一处能看全**「现有工具各是什么角色」—— 漏改一处就静默退化
（比如新 UI 忘了登记映射，前端只会收到 SHOW_TEXT）。

## 新增一个工具的完整清单（照着做，只碰这几处）

1. 在 `agent/tools.py`（或 `agent/skills/`）写函数 + `@register_tool`
2. 若它**结束本轮**（模型用它给出最终结论）→ 加进 :data:`TERMINAL_TOOLS`
   ⚠️ 同时检查它的 schema：**`reply` 必须排在 `properties` 第一位**
   （模型按参数顺序生成，reply 在后时首字要等 20s+，见 2026-09-20 延迟改造）
3. 若它**读**同轮其它工具写入的会话状态 → 加进 :data:`DEFERRED_TOOLS`
   （否则会被丢进并发批次，读到空状态）
4. 若它**产出方案卡** → 加进 :data:`CARD_TOOLS`（「该出卡却只写文字」护栏的判据）
5. 若它引入**新 UI 类型** → 在 `ui_protocol.UIType` 定义 + 加进 :data:`UI_TO_ACTION`
   （需要能力标签的再加 :data:`ACTION_CAPABILITY`）
6. 若新 UI 是**卡片类** → 加进 :data:`CARD_UIS`
7. 在 `agent/prompts/base.md` 的工具选择表里补一行

## 依赖

本文件**不 import `agent.agent`**（避免循环依赖），只依赖 `ui_protocol`。
"""
from __future__ import annotations

from agent.engine.ui_protocol import AgentActionType, UIType

# ── UI 分类 ──────────────────────────────────────────────────────────────────

#: 卡片类 UI。两处用到它：
#:   ① 「要点兜底」—— 带卡片但回复过短时补一句基于卡片数据的说明；
#:   ② 「独白兜底」—— 卡片场景要给卡片版文案，否则会出现「已为你准备好」（却没有卡片）。
CARD_UIS: tuple[UIType, ...] = (
    UIType.PLAN_CARD, UIType.SHOP_CARD, UIType.ORDER_CARD,
    UIType.PAY_JUMP, UIType.IMAGE_TASK, UIType.GREETING_CARD,
)

#: UI → 前端动作。接入方按 `action.type` 决定跳哪个页面。
#: ⚠️ 新增 UI 类型**必须**在这里登记，否则会静默退化成 `SHOW_TEXT`（用户看不到卡片）。
UI_TO_ACTION: dict[UIType, AgentActionType] = {
    UIType.PLAN_CARD: AgentActionType.SHOW_PLAN,
    UIType.SHOP_CARD: AgentActionType.SHOW_SHOP,
    UIType.ORDER_CARD: AgentActionType.CREATE_ORDER,
    UIType.PAY_JUMP: AgentActionType.OPEN_PAYMENT,
    UIType.IMAGE_TASK: AgentActionType.START_IMAGE_TASK,
    UIType.DIALOG_OPTIONS: AgentActionType.SHOW_OPTIONS,
    UIType.TEXT: AgentActionType.SHOW_TEXT,
}

#: Action → 接入方需具备的能力标签（写进 `/ui-contract`，接入方可据此降级）。
ACTION_CAPABILITY: dict[AgentActionType, str] = {
    AgentActionType.SHOW_PLAN: 'show_plan_page',
    AgentActionType.SHOW_SHOP: 'show_shop_page',
    AgentActionType.CREATE_ORDER: 'create_order',
    AgentActionType.OPEN_PAYMENT: 'open_payment',
    AgentActionType.START_IMAGE_TASK: 'start_image_task',
}

# ── 工具分类 ─────────────────────────────────────────────────────────────────

#: 终结工具族：调用其中任一 = 模型在本轮**选定了 UI 输出形态**并结束本轮。
#: 它们的入参本身就是结论（`reply` / `ui` / `data`），因此**不执行**、只取参数。
#: ⚠️ 参数顺序有性能含义：`reply` 必须排 schema 第一位（见模块 docstring 第 2 条）。
TERMINAL_TOOLS: tuple[str, ...] = ('respond_to_user', 'show_plan_card', 'show_options')

#: 延迟工具：**读**同轮其它工具写入的会话状态（生图要取「最近一次 DIY 方案」），
#: 必须排在并发批次之后单独执行，不能进 `asyncio.gather`。
DEFERRED_TOOLS: tuple[str, ...] = ('generate_effect_image',)

#: 产卡工具：调用后本轮应当产出方案卡。
#: 用于「该出卡却只写文字」护栏的判据 —— 用户被告知「明细在卡片里」而卡片不存在，
#: 是最危险的模型行为之一。
CARD_TOOLS: tuple[str, ...] = ('generate_diy_plan', 'revise_diy_plan', 'show_plan_card')
