"""知识库花材域：数据完整性 + 风险缺口 + 召回抽检（不依赖 DB/LLM/网络）。

护栏目标：
- 广度：扩充后花材 >= 42 种（16 原有 + 26 新增），防未来重构静默丢数据。
- 唯一性：id 不重复。
- 字段完整：每条含 11 个核心字段。
- 风险缺口：黄玫瑰/黄康乃馨的负面花语须以 caution 形式存在，玫瑰须含朵数含义。
- 召回：新增花材（芍药/澳梅/龙胆）能被精确查回。
"""

from pathlib import Path

import pytest

FLOWERS_PATH = Path(__file__).resolve().parent.parent / "agent" / "knowledge" / "flowers.json"

REQUIRED = [
    "id", "name", "aliases", "flower_language", "colors", "season",
    "price_tier", "freshness", "category", "pairing_notes", "tags",
]


def _load() -> list[dict]:
    return __import__("json").loads(FLOWERS_PATH.read_text(encoding="utf-8"))


def test_flowers_breadth_guard() -> None:
    data = _load()
    # 16 原有 + 26 新增 = 42；用 >= 允许后续继续扩充但不许回退。
    assert len(data) >= 42, f"花材数回退：{len(data)} < 42"


def test_no_duplicate_ids() -> None:
    ids = [d["id"] for d in _load()]
    dup = [i for i in set(ids) if ids.count(i) > 1]
    assert not dup, f"存在重复 id: {dup}"


def test_required_fields_present() -> None:
    for d in _load():
        missing = [k for k in REQUIRED if k not in d]
        assert not missing, f"{d.get('name', '?')} 缺字段: {missing}"


def test_risk_gap_caution_present() -> None:
    data = {d["name"]: d for d in _load()}
    # 黄玫瑰 = 失恋/嫉妒，必须以 caution 形式进入召回文本。
    assert "caution" in data["玫瑰"], "F_ROSE 缺 caution"
    assert "黄玫瑰" in data["玫瑰"]["caution"], "F_ROSE caution 未覆盖黄玫瑰风险"
    # 黄康乃馨 = 轻蔑/拒绝。
    assert "黄康乃馨" in data["康乃馨"]["caution"], "F_CARNATION caution 未覆盖黄康乃馨风险"
    # 玫瑰朵数含义（高价值广度内容）。
    assert "count_meaning" in data["玫瑰"] and len(data["玫瑰"]["count_meaning"]) >= 5


def test_retrieval_recall_new_flowers() -> None:
    from agent.knowledge.store import query_knowledge

    for q, expect in [("芍药", "芍药"), ("澳梅", "澳梅"), ("龙胆", "龙胆")]:
        r = query_knowledge(domain="flower", query=q)
        names = [x["name"] for x in r["results"]]
        assert expect in names, f"查询「{q}」未召回 {expect}，实际前 3: {names[:3]}"
