"""knowledge/store.py —— 花卉 DIY 知识库加载与向量混合检索。

域说明：
- 花材/风格/搭配/预算/包装/场景：JSON 文件域（flowers/styles/pairings/budget/packaging/scenes.json）。
- 商家智库（shop）：特殊域，数据来自 DB 的 shop_profiles 档案（含风格/场景名称），
  与 JSON 域共用同一套混合检索，AI 可用自然语言召回「韩式花店 / 能做婚礼布置的店」等商家实体。

升级说明（相对旧版纯关键词检索）：
- 检索升级为「向量空间模型（TF-IDF + 字符 n-gram 切词）+ 余弦相似度」的语义检索（RAG 思路），
  但对外接口 query_knowledge(domain, query) 的签名、返回结构完全不变，上层 tools.py / 工具零改动。
- 采用「关键词命中保底 ∪ 向量语义召回」的混合策略：
  * 关键词命中（旧的 _match 逻辑）一定被包含，保证既有精确查询（单 token 查找）零退化；
  * 仅在「多 token 或长自然语句」时，额外用向量相似度召回语义相关条目并参与排序，
    提升自然语言查询的召回与相关性（如 LLM 工具 retrieve_knowledge 传来的中文 NL 查询）。
- 纯 Python 实现（仅标准库 math/re/json），不引入 numpy/sklearn，保证 dev 零成本、可离线跑。
- settings.rag_enabled=False 时整体回退到旧关键词行为，可一键回滚。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from backend.config import settings

logger = logging.getLogger('knowledge')
_DOMAINS: dict[str, str] = {'flower': 'flowers.json', 'style': 'styles.json', 'pairing': 'pairings.json', 'budget': 'budget.json', 'packaging': 'packaging.json', 'scene': 'scenes.json', 'shop': '', 'proven': ''}
_BASE_DIR = Path(__file__).resolve().parent
_cache: dict[str, list[dict[str, Any]]] = {}
_index_cache: dict[str, _VectorSpace] = {}
_manifest_cache: dict[str, Any] | None = None
_CJK = re.compile('[\\u4e00-\\u9fff]+')
_WORD = re.compile('[a-zA-Z0-9]+')
_SEMANTIC_MIN_LEN = 6

def _tokenize(text: str) -> list[str]:
    """把文本切成检索特征：中文走字符 unigram+bigram，拉丁/数字作为整词小写。

    为什么用字符 n-gram：中文没有空格分词，字符 bigram 是成熟且零依赖的切词方式，
    能捕捉「探病/生病」「母亲/母爱」等局部字符共现，为向量检索提供可计算的相似度。
    """
    features: list[str] = []
    for m in _CJK.finditer(text or ''):
        run = m.group(0)
        features.extend(run)
        for i in range(len(run) - 1):
            features.append(run[i:i + 2])
    for m in _WORD.finditer(text or ''):
        features.append(m.group(0).lower())
    return features

class _VectorSpace:
    """极简 TF-IDF 向量空间：fit 语料后，similarity(query) 返回每条文档的余弦相似度。"""

    def __init__(self) -> None:
        self._vocab: dict[str, int] = {}
        self._idf: list[float] = []
        self._vecs: list[dict[int, float]] = []

    def fit(self, docs: list[str]) -> _VectorSpace:
        df: dict[str, int] = {}
        term_counts: list[dict[str, int]] = []
        for d in docs:
            tc: dict[str, int] = {}
            for f in _tokenize(d):
                tc[f] = tc.get(f, 0) + 1
            term_counts.append(tc)
            for t in tc:
                df[t] = df.get(t, 0) + 1
        self._vocab = {t: i for i, t in enumerate(df.keys())}
        n = len(docs)
        self._idf = [math.log((n + 1) / (df[t] + 1)) + 1.0 for t in self._vocab]
        self._vecs = []
        for tc in term_counts:
            vec: dict[int, float] = {}
            length = 0.0
            total = sum(tc.values()) or 1
            for t, c in tc.items():
                idx = self._vocab[t]
                w = c / total * self._idf[idx]
                vec[idx] = w
                length += w * w
            norm = math.sqrt(length) or 1.0
            self._vecs.append({k: v / norm for k, v in vec.items()})
        return self

    def similarity(self, query: str) -> list[float]:
        """返回 query 与每条文档的余弦相似度（已归一化向量 → 点积即余弦）。"""
        tc: dict[str, int] = {}
        for f in _tokenize(query):
            tc[f] = tc.get(f, 0) + 1
        qvec: dict[int, float] = {}
        qlen = 0.0
        total = sum(tc.values()) or 1
        for t, c in tc.items():
            if t in self._vocab:
                idx = self._vocab[t]
                w = c / total * self._idf[idx]
                qvec[idx] = w
                qlen += w * w
        if not qvec:
            return [0.0] * len(self._vecs)
        qnorm = math.sqrt(qlen)
        scores: list[float] = []
        for vec in self._vecs:
            if len(qvec) <= len(vec):
                dot = sum((w / qnorm * vec[idx] for idx, w in qvec.items() if idx in vec))
            else:
                dot = sum((vec[idx] / qnorm * w for idx, w in qvec.items() if idx in vec))
            scores.append(dot)
        return scores

def _load_shops() -> list[dict[str, Any]]:
    """惰性加载商家智库档案（来自 DB 的 shop_profiles，含风格/场景名称）。

    惰性 import backend.storage as storage.catalog：catalog 仅在函数内引用 knowledge，
    双向依赖均为运行时导入，无循环依赖问题。
    """
    if 'shop' in _cache:
        return _cache['shop']
    data: list[dict[str, Any]] = []
    try:
        # 独立封装版暂不内置商家智库，保持空结果，避免依赖原项目完整 catalog 层。
        data = []
    except Exception:
        logger.warning('[knowledge] 商家智库加载失败（DB 未就绪？）', exc_info=True)
    _cache['shop'] = data
    return data

def _run_async(coro):
    """在「当前是否已有事件循环」都安全的情况下运行协程。

    - 无运行中的循环（模块加载 / run() 所在 worker 线程 / 同步工具）：直接 asyncio.run。
    - 已有运行中的循环（未来在异步工具内调用）：临时起一个线程跑独立事件循环，
      避免 "loop is already running"。
    - 结束前清理临时循环创建的 PG 引擎，避免临时 loop 关闭后残留死连接累积
      （全套测试末尾出现的 ConnectionDoesNotExistError 即源于此）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()

def _load_proven() -> list[dict[str, Any]]:
    """实战方案域：diy_plans 中高确认/高成交的用户方案（平台学习素材）。

    注意：**不做内存缓存**——proven 随用户确认/成交动态增长，缓存会导致
    新增方案不可见（测试与线上行为都会受影响）。
    """
    data: list[dict[str, Any]] = []
    try:
        from backend.storage.diy import list_proven_plans
        data = list_proven_plans()
    except Exception:
        logger.warning('[knowledge] 实战方案库加载失败（DB 未就绪？）', exc_info=True)
    return data

def _load(domain: str) -> list[dict[str, Any]]:
    """加载某域数据（带内存缓存，避免重复读盘）。proven 域动态增长，不缓存。"""
    if domain == 'proven':
        return _load_proven()
    if domain in _cache:
        return _cache[domain]
    if domain == 'shop':
        return _load_shops()
    path = _BASE_DIR / _DOMAINS[domain]
    if not path.exists():
        logger.warning('[knowledge] 数据文件缺失: %s', path)
        _cache[domain] = []
        return _cache[domain]
    with path.open(encoding='utf-8') as f:
        data = json.load(f)
    _cache[domain] = data
    return data

def load_manifest() -> dict[str, Any]:
    """读取知识库清单。"""
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache
    path = _BASE_DIR / 'knowledge_manifest.json'
    if not path.exists():
        _manifest_cache = {}
        return _manifest_cache
    with path.open(encoding='utf-8') as f:
        _manifest_cache = json.load(f)
    return _manifest_cache

def _collect_strings(value: Any, out: list[str]) -> None:
    """递归收集 dict/list 里的所有字符串（嵌套结构如 styles/scenes 名称也纳入索引文本）。"""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_strings(v, out)

def _entry_text(entry: dict[str, Any]) -> str:
    """把一条知识的可检索字段拼成一段文本，用于构建向量（递归含嵌套名称）。"""
    parts: list[str] = []
    for v in entry.values():
        _collect_strings(v, parts)
    return ' '.join(parts)

def _get_index(domain: str) -> _VectorSpace:
    """懒构建并按域缓存向量空间（首次非关键词查询时构建）。"""
    if domain not in _index_cache:
        _index_cache[domain] = _VectorSpace().fit([_entry_text(e) for e in _load(domain)])
    return _index_cache[domain]

def _match(entry: dict[str, Any], tokens: list[str]) -> bool:
    """entry 是否命中任一 token：匹配 name/aliases/tags/flower_language/colors 等文本字段。"""
    haystack = ' '.join((str(v) for k, v in entry.items() if isinstance(v, (str, list)) for v in ([v] if isinstance(v, str) else v)))
    return any(tok in haystack for tok in tokens)

def _allow_vector(query: str, tokens: list[str]) -> bool:
    """是否启用向量语义扩展：仅当 rag 开启，且查询为「多 token 或长自然语句」。

    短单 token（如「母亲节」「康乃馨」「妈妈」）一律走精确关键词，保证内部查找与旧测试零退化。
    """
    return settings.rag_enabled and (len(tokens) >= 2 or len(query) >= _SEMANTIC_MIN_LEN)

def _retrieve_domain(domain: str, tokens: list[str], allow_vector: bool) -> list[tuple[dict[str, Any], float]]:
    """单域检索：返回 [(entry, score)]，按 score 降序。

    - allow_vector=False：仅关键词命中（= 旧行为），score 记为 1.0。
    - allow_vector=True：关键词命中 ∪ 向量相似度≥阈值的条目；关键词命中额外加成，排序更相关。
    """
    entries = _load(domain)
    if not entries:
        return []
    kw_hits = [_match(e, tokens) for e in entries]
    if not allow_vector:
        return [(e, 1.0) for e, hit in zip(entries, kw_hits, strict=True) if hit]
    sims = _get_index(domain).similarity(' '.join(tokens))
    out: list[tuple[dict[str, Any], float]] = []
    for e, hit, sim in zip(entries, kw_hits, sims, strict=True):
        score = float(sim)
        if hit:
            score += settings.rag_keyword_boost
        if hit or score >= settings.rag_min_score:
            out.append((e, score))
    out.sort(key=lambda x: x[1], reverse=True)
    if settings.rag_top_k and len(out) > settings.rag_top_k:
        out = out[:settings.rag_top_k]
    return out

def query_knowledge(domain: str='all', query: str='') -> dict[str, Any]:
    """知识库检索（向量混合检索，接口向后兼容）。

    Args:
        domain: "all" 或 flower/style/pairing/budget/packaging/scene/shop 之一。
        query: 自然语言或关键词。
            - 空串：返回该域全部（上层枚举场景/风格/预算档依赖此行为）。
            - 短单 token：精确关键词匹配（向后兼容内部查找与旧测试）。
            - 多 token / 长自然语句：关键词命中 ∪ 向量语义召回，按相关度排序。

    Returns:
        { "domain": str, "query": str, "count": int, "results": [ {_domain, _score, ...entry} ] }
        新增 _score 字段（相似度/相关性，仅用于排序与可解释性，不影响既有字段读取）。
    """
    tokens = [t for t in query.replace(',', ' ').split() if t]
    domains = list(_DOMAINS) if domain == 'all' else [domain]
    if not tokens:
        results = []
        for dom in domains:
            if dom not in _DOMAINS:
                continue
            for entry in _load(dom):
                results.append({'_domain': dom, '_score': 1.0, **entry})
        return {'domain': domain, 'query': query, 'count': len(results), 'results': results}
    allow_vector = _allow_vector(query, tokens)
    results = []
    for dom in domains:
        if dom not in _DOMAINS:
            continue
        for entry, score in _retrieve_domain(dom, tokens, allow_vector):
            results.append({'_domain': dom, '_score': round(score, 4), **entry})
    if domain == 'all':
        results.sort(key=lambda r: r['_score'], reverse=True)
    return {'domain': domain, 'query': query, 'count': len(results), 'results': results}

def get_by_id(domain: str, item_id: str) -> dict[str, Any] | None:
    """按 id 精确取一条知识（设计函数内部用）。"""
    for entry in _load(domain):
        if entry.get('id') == item_id:
            return entry
    return None
