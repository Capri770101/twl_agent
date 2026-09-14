"""语义相似度通道（Tier 1）：用真实 embedding 计算「条目 vs 查询」的余弦相似度。

设计：
- 条目向量**惰性构建并缓存**到 `data/embed_cache.json`（运行时产物，gitignored），
  以「模型 + 域名 + 条目内容哈希」为键 —— KB 内容一变，哈希变，缓存自动失效重建。
- 查询向量每次实时计算（每条查询 1 次 API 调用）。
- 纯 Python 余弦，不引入 numpy（与知识库其余部分一致）。
- **任何一环失败 → 返回 None**，store 回退纯 TF-IDF，绝不影响检索可用性。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from pathlib import Path

from backend.config import settings

logger = logging.getLogger('knowledge.semantic')
_LOCK = threading.Lock()
_MAX_CACHE_KEYS = 40


def _cache_file() -> Path:
    root = Path(__file__).resolve().parent.parent.parent
    return root / 'data' / 'embed_cache.json'


def _cosine(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度（零依赖）。"""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = na = nb = 0.0
    for i in range(n):
        x = a[i]
        y = b[i]
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return dot / denom if denom else 0.0


def _load_cache() -> dict:
    p = _cache_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        logger.warning('[semantic] 缓存解析失败，视为空')
        return {}


def _save_cache(cache: dict) -> None:
    try:
        with _LOCK:
            p = _cache_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
    except Exception:  # noqa: BLE001
        logger.warning('[semantic] 缓存写入失败', exc_info=True)


def _domain_texts(domain: str) -> list[str]:
    """取该域每条条目的可检索文本（复用 store 的递归拼装，保证与 TF-IDF 同一语料）。"""
    from agent.knowledge.store import _entry_text, _load
    return [_entry_text(e) for e in _load(domain)]


def _content_key(domain: str, texts: list[str]) -> str:
    h = hashlib.sha256('|'.join(texts).encode('utf-8')).hexdigest()[:16]
    return f"{settings.EMBEDDING_MODEL}:{domain}:{h}"


def domain_similarities(domain: str, query: str) -> list[float] | None:
    """返回该域每条条目与 query 的余弦相似度（顺序与 `_load(domain)` 一致）；不可用返回 None。"""
    if not settings.embedding_enabled or not query:
        return None
    try:
        from backend.embedding import embed_texts

        texts = _domain_texts(domain)
        if not texts:
            return None
        key = _content_key(domain, texts)

        cache = _load_cache()
        entry = cache.get(key)
        doc_vecs = None
        if isinstance(entry, dict) and isinstance(entry.get('vectors'), list) and len(entry['vectors']) == len(texts):
            doc_vecs = entry['vectors']
        if doc_vecs is None:
            doc_vecs = embed_texts(texts, 'document')
            if doc_vecs is None:
                return None
            cache = _load_cache()
            cache[key] = {'model': settings.EMBEDDING_MODEL, 'domain': domain, 'vectors': doc_vecs}
            if len(cache) > _MAX_CACHE_KEYS:
                for k in list(cache.keys())[:-_MAX_CACHE_KEYS]:
                    cache.pop(k, None)
            _save_cache(cache)

        qvec = embed_texts([query], 'query')
        if not qvec:
            return None
        q = qvec[0]
        return [_cosine(v, q) for v in doc_vecs]
    except Exception:  # noqa: BLE001
        logger.warning('[semantic] 语义相似度计算失败，回退 TF-IDF', exc_info=True)
        return None
