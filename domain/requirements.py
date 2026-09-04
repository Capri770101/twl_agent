"""共享领域模型：结构化需求。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FlowerRequirement:
    """用户送花需求的结构化表示。"""

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
    stem_count: int | None = None  # 用户明确的花材支数（如「11 朵」），与预算无关
    single_flower: str | None = None  # 用户要求单一花材（如「纯红玫瑰」），值为花名
    location: dict[str, float] | None = None
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> FlowerRequirement:
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
            stem_count=d.get("stem_count"),
            single_flower=d.get("single_flower"),
            location=d.get("location"),
            raw=d.get("raw", ""),
        )

    def to_legacy_dict(self) -> dict[str, str]:
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
        if self.stem_count is not None:
            d["stem_count"] = str(int(self.stem_count))
        if self.single_flower:
            d["single_flower"] = self.single_flower
        return d

    def merge(self, other: FlowerRequirement) -> FlowerRequirement:
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
        if other.stem_count is not None:
            merged.stem_count = other.stem_count
        if other.single_flower:
            merged.single_flower = other.single_flower
        if other.location:
            merged.location = other.location
        return merged
