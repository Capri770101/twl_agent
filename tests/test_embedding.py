"""embedding 语义通道测试（用 mock provider，离线可跑，不烧 API）。

覆盖：
- 关闭时 embed_texts 返回 None（回退）
- mock 向量确定性 + 归一化 + 维度正确
- semantic 通道返回形状正确、缓存落 tmp
- store 集成：关闭时行为不变；开启时语义通道可注入召回（用受控相似度验证并集逻辑）
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.knowledge import semantic, store
from backend import embedding
from backend.config import settings


@pytest.fixture
def mock_embedding(monkeypatch, tmp_path):
    """开启 mock provider，并把语义缓存改到 tmp（不污染仓库 data/）。"""
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "mock")
    monkeypatch.setattr(settings, "EMBEDDING_DIM", 64)
    monkeypatch.setattr(semantic, "_cache_file", lambda: tmp_path / "embed_cache.json")
    return tmp_path


def test_embed_off_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "")
    assert embedding.embed_texts(["x"]) is None


def test_mock_vector_deterministic_normalized(mock_embedding):
    v1 = embedding.embed_texts(["玫瑰"])
    v2 = embedding.embed_texts(["玫瑰"])
    assert v1 is not None and len(v1) == 1 and len(v1[0]) == settings.EMBEDDING_DIM
    assert v1 == v2
    norm = math.sqrt(sum(x * x for x in v1[0]))
    assert abs(norm - 1.0) < 1e-6
    # 不同文本向量不同
    assert embedding.embed_texts(["百合"]) != v1


def test_semantic_similarities_shape(mock_embedding):
    sims = semantic.domain_similarities("flower", "玫瑰")
    assert sims is not None
    assert len(sims) == len(store._load("flower"))
    assert all(isinstance(s, float) for s in sims)
    # 缓存已落盘
    assert (mock_embedding / "embed_cache.json").exists()


def test_semantic_cache_reused(mock_embedding):
    semantic.domain_similarities("style", "韩式")
    first = (mock_embedding / "embed_cache.json").read_text(encoding="utf-8")
    semantic.domain_similarities("style", "极简")
    second = (mock_embedding / "embed_cache.json").read_text(encoding="utf-8")
    # 同域内容哈希一致 → 第二次不应新增 key（缓存复用）
    assert first == second


def test_store_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "")
    res = store.query_knowledge("flower", "玫瑰")
    assert res["count"] >= 1


def test_merge_injects_embedding_recall(mock_embedding, monkeypatch):
    # 受控语义通道：只让第 0 条高分，其余 0 → 该条应在无关键词命中时被召回
    entries = store._load("care")
    fake = [0.0] * len(entries)
    fake[0] = 0.9
    monkeypatch.setattr(semantic, "domain_similarities", lambda d, q: list(fake))
    out = store._retrieve_domain("care", ["zzznotokenzzz"], True)
    ids = [e.get("id") for e, _s in out]
    assert entries[0].get("id") in ids
