"""engine/state.py —— 会话焦点（Focus）枚举。

⚠️ 本模块**不再承担流程机职责**。

早期版本用 `_ALLOWED` 邻接表 + `can_transition()` 硬锁每一步流转。自
「skill 编排」重构后，流程改由 **ReAct 循环 + 工具产物依赖**驱动：模型可随时
调用任一技能（设计 / 生图 / 搜店 / 改方案），不再受阶段邻接约束。那套邻接表
与校验函数已彻底删除。

因此这里仅保留 `SessionStage` 枚举，语义收窄为 **Focus（UI 高亮用）**：
- 值仍是小写字符串，直接存库、作为前端进度标识；
- 仅表示「用户当前在干嘛」的 UI 展示，**不参与任何流程闸门 / 流转校验**。
"""

from __future__ import annotations

from enum import StrEnum


class SessionStage(StrEnum):
    """导购会话焦点。值为小写字符串，直接存库、也可作为前端进度标识。"""

    ANALYZE = "analyze"              # 理解需求，提取预算/对象/偏好
    SELECT_MODE = "select_mode"      # 弹出「现有方案 / DIY」二选一
    VIEW_PLAN = "view_plan"          # 浏览商家预设方案
    DIY_DESIGN = "diy_design"        # 设计 DIY 方案
    IMAGE_GEN = "image_gen"          # DIY 方案异步生图
    PLAN_CONFIRM = "plan_confirm"    # 用户确认方案
    SHOP_RECOMMEND = "shop_recommend"  # 推荐店铺
    ORDER_CONFIRM = "order_confirm"  # 组装订单
    DONE = "done"                    # 已生成支付跳转参数
    GREETING_CARD = "greeting_card"  # 为订单配电子贺卡 / 生成贺卡图

    @classmethod
    def ordered(cls) -> list[SessionStage]:
        """业务正序，仅供 UI 焦点排序参考，不代表强制流程。"""
        return [
            cls.ANALYZE, cls.SELECT_MODE, cls.VIEW_PLAN, cls.DIY_DESIGN,
            cls.IMAGE_GEN, cls.PLAN_CONFIRM, cls.SHOP_RECOMMEND,
            cls.ORDER_CONFIRM, cls.DONE, cls.GREETING_CARD,
        ]
