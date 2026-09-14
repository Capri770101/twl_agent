"""场景域 / 搭配域 广度护栏 + 新场景识别测试。

不依赖 DB / LLM / 网络：直读 JSON + 复用 store.query_knowledge 的场景识别链路。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.knowledge.store import query_knowledge

_KB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent", "knowledge")


def _load(name: str) -> list:
    with open(os.path.join(_KB, f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


def _flower_names() -> set:
    return {e["name"] for e in _load("flowers")}


def test_scene_breadth_min_24():
    """场景域扩到 >=24 条（原 14 + 新增 10）。"""
    scenes = _load("scenes")
    assert len(scenes) >= 24, f"场景域应 >=24，实际 {len(scenes)}"


def test_pairing_breadth_min_24():
    """搭配域扩到 >=24 条（原 12 + 新增 12）。"""
    pairings = _load("pairings")
    assert len(pairings) >= 24, f"搭配域应 >=24，实际 {len(pairings)}"


def test_new_scene_ids_present():
    """10 个新场景 ID 齐备。"""
    ids = {e["id"] for e in _load("scenes")}
    for sid in [
        "SC_PROPOSE", "SC_ENGAGEMENT", "SC_NEWBABY", "SC_OPENING", "SC_EXAM",
        "SC_CONDOLENCE", "SC_MIDAUTUMN", "SC_MILESTONE", "SC_MISS", "SC_THANKS",
    ]:
        assert sid in ids, f"缺失新场景 {sid}"


def test_new_scene_pref_resolves():
    """新场景 main_flower_preference 必须用真实花名（通用名），否则偏好静默失效。"""
    flowers = _flower_names()
    for s in _load("scenes"):
        if s["id"].startswith(("SC_PROPOSE", "SC_ENGAGEMENT", "SC_NEWBABY", "SC_OPENING",
                               "SC_EXAM", "SC_CONDOLENCE", "SC_MIDAUTUMN", "SC_MILESTONE",
                               "SC_MISS", "SC_THANKS")):
            missing = [n for n in s.get("main_flower_preference", []) if n not in flowers]
            assert not missing, f"{s['id']} 偏好含不存在花名: {missing}"


def test_new_pairing_ids_present():
    """12 条新搭配规则 ID 齐备。"""
    ids = {e["id"] for e in _load("pairings")}
    for pid in [
        "P_COLOR_TRIADIC", "P_COLOR_CHAMPAGNE", "P_SHAPE_ROUND", "P_SHAPE_CASCADE",
        "P_OCC_PROPOSE", "P_OCC_NEWBABY", "P_OCC_OPENING", "P_OCC_EXAM",
        "P_OCC_CONDOLENCE", "P_RECIP_ELDER", "P_RECIP_COLLEAGUE", "P_MAT_FOLIAGE",
    ]:
        assert pid in ids, f"缺失新搭配 {pid}"


@pytest.mark.parametrize("query,expected_scene", [
    ("满月酒", "新生儿 / 满月 / 百日"),
    ("开业花篮", "开业 / 商务庆典"),
    ("升学宴", "升学 / 考试 / 金榜题名"),
    ("中秋节", "中秋 / 团圆"),
    ("订婚宴", "订婚"),
    ("白事", "清明 / 祭奠 / 哀悼"),
    ("想念", "思念 / 异地 / 想你"),
])
def test_scene_detection_compound(query, expected_scene):
    """复合自然语言问句能被正确识别到对应场景（中文无空格的子串识别）。"""
    r = query_knowledge("scene", query)["results"]
    assert r, f"场景识别 0 召回: {query}"
    assert r[0]["name"] == expected_scene, f"[{query}] 期望 {expected_scene}，实际 {r[0]['name']}"
