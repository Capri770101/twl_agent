"""知识库检索质量 + 薄域广度护栏（不依赖 DB/LLM/网络）。

护栏目标：
- 同义词扩展：中文 NL 词汇对齐（治「简约/流行」等召回脆性根因）。
- 薄域广度：styles >= 12 / budget >= 6 / packaging >= 6，防重构静默回退。
- 预算档：6 档（T1-T6）按 range 命中且默认档位合理。
- 「流行」概念：趋势花材带 热门/爆款/网红 标签且能被「流行」召回。
"""

from pathlib import Path

import pytest

KB = Path(__file__).resolve().parent.parent / "agent" / "knowledge"


def _load(name: str) -> list[dict]:
    return __import__("json").loads((KB / f"{name}.json").read_text(encoding="utf-8"))


# ---------- 同义词扩展（核心新能力，单元测试） ----------

def test_expand_query_synonyms() -> None:
    from agent.knowledge.store import _expand_query

    # 简约 → 同组词被追加（极简/性冷淡/冷淡风...）
    exp = _expand_query("简约")
    assert "极简" in exp and "性冷淡" in exp, f"简约未扩展同义词: {exp}"
    # 流行 → 热门/爆款/网红
    exp = _expand_query("最近流行")
    assert "热门" in exp and "爆款" in exp and "网红" in exp
    # 无同义词的原词不改
    assert _expand_query("玫瑰") == "玫瑰"
    # 空串安全
    assert _expand_query("") == ""


def test_synonym_minimal_style_recall() -> None:
    """「简约风格」此前相似度 0.0726 卡阈值，扩展后应召回极简系风格。"""
    from agent.knowledge.store import query_knowledge

    r = query_knowledge(domain="style", query="简约风格")
    names = [x["name"] for x in r["results"]]
    minimal = {"北欧", "日式", "韩式", "ins风"}
    assert minimal & set(names), f"「简约风格」未召回极简系风格，实际: {names[:5]}"


def test_synonym_popular_flower_recall() -> None:
    """「流行」此前无概念，扩展 + 标签后须召回带「热门」标签的趋势花材。"""
    from agent.knowledge.store import query_knowledge

    r = query_knowledge(domain="flower", query="最近流行什么样的花")
    assert r["count"] > 0, "「流行」查询无结果"
    popular = [x["name"] for x in r["results"] if "热门" in (x.get("tags") or [])]
    assert popular, "「流行」未召回任何带「热门」标签的趋势花材"


def test_popular_tag_exists() -> None:
    """至少 10 种花材标注了 热门/爆款/网红（流行概念的数据底座）。"""
    data = _load("flowers")
    n = sum(1 for d in data if {"热门", "爆款", "网红"} & set(d.get("tags", [])))
    assert n >= 10, f"带流行标签的花材仅 {n} 种（期望>=10）"


# ---------- 薄域广度护栏 ----------

def test_styles_breadth() -> None:
    assert len(_load("styles")) >= 12, "风格域回退到 < 12"


def test_budget_breadth() -> None:
    assert len(_load("budget")) >= 6, "预算域回退到 < 6 档"


def test_packaging_breadth() -> None:
    assert len(_load("packaging")) >= 6, "包装域回退到 < 6 款"


def test_budget_tiers_resolve() -> None:
    """6 档按 range 命中，且包含 T1-T6 主键。"""
    from agent.knowledge.store import query_knowledge

    tiers = query_knowledge(domain="budget", query="")["results"]
    assert len(tiers) >= 6, f"预算档仅 {len(tiers)} 档"
    ids = {t["tier"] for t in tiers}
    assert {"T1", "T2", "T3", "T4", "T5", "T6"} <= ids, f"缺预算档主键: {ids}"
    # 边界命中（升序，第 2 档作为默认）
    by_range = sorted(tiers, key=lambda t: t["range"][0])
    assert by_range[1]["range"][0] >= 150, "默认档位不应是最低档"
