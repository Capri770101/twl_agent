"""requirements.py —— 结构化需求状态 FlowerRequirement。

把「用户自然语言需求」收敛为一等公民的结构化对象，供 DIY 设计、方案检索、
店铺检索共享消费，而不是把整段原文甩给数据库或只散落在 LLM 的 context 里。

这是 review 中点名的关键缺口：当前需求信息只活在 LLM 对话上下文 / 零散的
save_memory KV 里，没有跨工具共享的结构化对象。抽出 FlowerRequirement 后，
search_plans / list_shops 才能按预算 / 色系 / 风格做真实过滤与排序。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FlowerRequirement:
    """用户送花需求的结构化表示（跨工具共享，可持久化到会话记忆）。

    Attributes:
        recipient: 送谁（母亲/恋人/朋友/自己/长辈/宝宝）
        relationship: 关系（亲子/情侣/朋友/同事/自用），多由 recipient 推导
        occasion: 场合（生日/母亲节/婚礼/探病…）
        style: 风格 id（S_KOREAN 等）
        substyle: 细分风格 id
        scene: 场景模板 id
        colors: 期望色系列表（红/粉/香槟…），支持多色
        mood: 氛围（温柔/浪漫/高级…）
        budget_num: 抽取到的精确预算金额（元），无则为 None
        budget_min / budget_max: 由 budget_num 推导的预算区间（±20%）
        budget_anchor: 口语表述原文（如「两三百」），用于回退展示
        location: 配送坐标 {lat, lng}，来自 API 或用户表达
        raw: 原始需求文本
    """

    recipient: str | None = None
    relationship: str | None = None
    occasion: str | None = None
    style: str | None = None
    substyle: str | None = None
    scene: str | None = None
    colors: list[str] = field(default_factory=list)
    mood: str | None = None
    budget_num: float | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    budget_anchor: str | None = None
    location: dict[str, float] | None = None
    raw: str = ""

    # --------------------------------------------------------------- #
    # 序列化 / 兼容
    # --------------------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        """转 dict，便于存 SQLite / 走 JSON。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> FlowerRequirement:
        """从 dict 重建（容忍缺字段，老数据兼容）。"""
        if not d:
            return cls()
        return cls(
            recipient=d.get("recipient"),
            relationship=d.get("relationship"),
            occasion=d.get("occasion"),
            style=d.get("style"),
            substyle=d.get("substyle"),
            scene=d.get("scene"),
            colors=list(d.get("colors") or []),
            mood=d.get("mood"),
            budget_num=d.get("budget_num"),
            budget_min=d.get("budget_min"),
            budget_max=d.get("budget_max"),
            budget_anchor=d.get("budget_anchor"),
            location=d.get("location"),
            raw=d.get("raw", ""),
        )

    def to_legacy_dict(self) -> dict[str, str]:
        """兼容旧 _extract 返回形态（DIY 设计管线 design_diy_plan 依赖）。

        只产出 recipient/occasion/style/color/mood/scene/budget 这些字符串键，
        与历史 _extract 输出逐字段对齐，保证既有测试与 _build_plan 零改动。
        """
        d: dict[str, str] = {}
        if self.recipient:
            d["recipient"] = self.recipient
        if self.occasion:
            d["occasion"] = self.occasion
        if self.style:
            d["style"] = self.style
        if self.substyle:
            d["substyle"] = self.substyle
        if self.scene:
            d["scene"] = self.scene
        if self.colors:
            d["color"] = self.colors[0]
        if self.mood:
            d["mood"] = self.mood
        if self.budget_num is not None:
            d["budget"] = str(int(self.budget_num))
        elif self.budget_anchor:
            d["budget"] = self.budget_anchor
        return d

    # --------------------------------------------------------------- #
    # 多轮累加
    # --------------------------------------------------------------- #

    def merge(self, other: FlowerRequirement) -> FlowerRequirement:
        """与另一轮抽取结果累加：other 中非空字段覆盖 self。

        用于跨多轮对话累积需求（如第一轮说预算、第二轮说对象），
        而不是每轮从零抽取覆盖。
        """
        merged = FlowerRequirement(**asdict(self))
        for f in ("recipient", "relationship", "occasion", "style", "substyle", "scene", "mood", "budget_anchor", "raw"):
            v = getattr(other, f)
            if v:
                setattr(merged, f, v)
        if other.colors:
            merged.colors = list(dict.fromkeys(merged.colors + other.colors))
        if other.budget_num is not None:
            merged.budget_num = other.budget_num
            merged.budget_min = other.budget_min
            merged.budget_max = other.budget_max
        if other.location:
            merged.location = other.location
        return merged
