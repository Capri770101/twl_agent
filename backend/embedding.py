"""文本向量化客户端（Tier 1 · 真实 embedding 升级）。

- `provider=dashscope`：走阿里云百炼 **OpenAI 兼容** `/embeddings`（与 LLM 同厂商，key 可复用）。
- `provider=mock`：确定性伪向量，**仅供测试**（保证测试离线可跑、不烧 API）。
- 任何失败（未配置 / 网络 / 非 2xx / 条数不符）→ 返回 ``None``，调用方**回退纯 TF-IDF**，
  绝不因 embedding 故障影响检索主流程。

注意：真实调用有成本与延迟，因此默认 `EMBEDDING_PROVIDER=""` 关闭；开启前用评测集标定。
"""
from __future__ import annotations

import hashlib
import logging
import math

from backend.config import settings

logger = logging.getLogger('embedding')

# 百炼 text-embedding-v3 单次批量上限较保守，按 10 条切块，避免超限报错
_BATCH = 10


def _mock_vector(text: str, dim: int) -> list[float]:
    """确定性伪向量（仅测试用）：按「字符 unigram+bigram」哈希分桶。

    这样**重叠文本相似度高、无关文本近似正交**，行为上更接近真实 embedding——
    避免在低维下稠密向量互相虚高相似度而失真。同文本永远同向量。
    """
    vec = [0.0] * dim
    chars = [c for c in text if not c.isspace()]
    grams = list(chars) + [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    for g in grams:
        idx = int(hashlib.md5(g.encode('utf-8')).hexdigest(), 16) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_texts(texts: list[str], text_type: str = 'document') -> list[list[float]] | None:
    """把文本列表转成向量列表（与输入等长）。

    Args:
        texts: 待向量化文本。
        text_type: 'document' | 'query'（百炼原生接口区分用途；兼容接口暂忽略）。
    Returns:
        向量列表；功能关闭或调用失败时返回 ``None``（调用方据此回退）。
    """
    if not texts:
        return []
    provider = (settings.EMBEDDING_PROVIDER or '').strip().lower()
    if provider in ('', 'off', 'none', 'false', '0'):
        return None
    if provider == 'mock':
        return [_mock_vector(t, settings.EMBEDDING_DIM) for t in texts]
    if provider == 'dashscope':
        return _embed_dashscope(texts, text_type)
    logger.warning('[embedding] 未知 provider=%s，回退', provider)
    return None


def _embed_dashscope(texts: list[str], text_type: str) -> list[list[float]] | None:
    """调用百炼 OpenAI 兼容 /embeddings，分批取回向量。失败返回 None。"""
    key = settings.embedding_api_key
    base = settings.embedding_base_url
    if not key or not base:
        logger.warning('[embedding] 缺 api_key / base_url，回退')
        return None
    # 护栏：疑似占位符/非法地址直接跳过，避免误配时刷一屏无意义的连接异常
    if not base.startswith(('http://', 'https://')):
        logger.warning('[embedding] base_url 非 http(s)，跳过：%s', base[:32])
        return None
    probe = (base + ' ' + key).lower()
    if any(t in probe for t in ('replace-me', 'your-', 'change-me', 'xxx', 'todo')):
        logger.warning('[embedding] 疑似占位符凭据，跳过 embedding（回退 TF-IDF）')
        return None
    import httpx

    url = base.rstrip('/') + '/embeddings'
    vectors: list[list[float]] = []
    try:
        with httpx.Client(timeout=30.0) as client:
            for start in range(0, len(texts), _BATCH):
                chunk = texts[start:start + _BATCH]
                payload = {'model': settings.EMBEDDING_MODEL, 'input': chunk, 'encoding_format': 'float'}
                resp = client.post(url, headers={'Authorization': f'Bearer {key}'}, json=payload)
                if resp.status_code >= 400:
                    logger.warning('[embedding] dashscope HTTP %s: %s', resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
                items = data.get('data') or []
                if len(items) != len(chunk):
                    logger.warning('[embedding] 返回条数不符：%s != %s', len(items), len(chunk))
                    return None
                ordered = sorted(items, key=lambda x: x.get('index', 0))
                for it in ordered:
                    emb = it.get('embedding') or []
                    if not emb:
                        logger.warning('[embedding] 空向量')
                        return None
                    vectors.append([float(x) for x in emb])
    except Exception:  # noqa: BLE001
        logger.warning('[embedding] dashscope 调用失败', exc_info=True)
        return None
    return vectors
