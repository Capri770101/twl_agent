"""检索缺口日志（Tier 1.3）：记录「零结果 / 低分」查询，用于发现知识库盲区。

设计取舍：
- **默认关闭**（`RAG_GAP_LOG_ENABLED=false`）——不给每次检索都加写盘开销与隐私面；
  运营需要观测盲区时再打开。
- 仅记录**用户触发的自然语言查询**（query 非空），内部枚举调用（query 空）不记。
- 落盘 JSONL（append 友好），交给 `scripts/report_gaps.py` 聚合。
- 记录的是查询文本，属**内部日志**（仅用于补知识库），不对外展示。

与「人工审核」无关：本模块只做**观测**，不自动改知识库——符合「无人工审核」下
「先看见盲区再决定补什么」的定位。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger('knowledge.gap')
_LOCK = threading.Lock()
_DEFAULT_NAME = 'data/eval/retrieval_gaps.jsonl'


def _resolve_path() -> Path:
    """缺口日志路径：优先 settings.RAG_GAP_LOG_PATH，否则仓库内 data/eval/retrieval_gaps.jsonl。"""
    from backend.config import settings
    raw = (getattr(settings, 'RAG_GAP_LOG_PATH', '') or '').strip()
    if raw:
        return Path(raw)
    root = Path(__file__).resolve().parent.parent.parent
    return root / _DEFAULT_NAME


def record_gap(query: str, domain: str, n_results: int, best_score: float, path: Path | None = None) -> None:
    """追加一条缺口记录；失败仅告警，绝不影响检索主流程。path 仅供测试注入。"""
    if not query or not query.strip():
        return
    rec = {
        'ts': datetime.now(UTC).isoformat(timespec='seconds'),
        'query': query.strip()[:200],
        'domain': domain,
        'n_results': int(n_results),
        'best_score': round(float(best_score), 4),
    }
    try:
        target = path or _resolve_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            with target.open('a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:  # noqa: BLE001
        logger.warning('[knowledge] 缺口日志写入失败', exc_info=True)


def load_gaps(path: Path | None = None) -> list[dict]:
    """读取缺口日志（容错：跳过坏行）。"""
    p = path or _resolve_path()
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def aggregate(path: Path | None = None, top: int = 20) -> list[dict]:
    """按查询文本聚合，返回 [{query, count, last_ts, domains}] 按频次降序。"""
    counts: dict[str, dict] = {}
    for r in load_gaps(path):
        q = str(r.get('query') or '').strip()
        if not q:
            continue
        d = counts.setdefault(q, {'query': q, 'count': 0, 'last_ts': '', 'domains': set()})
        d['count'] += 1
        d['last_ts'] = max(d['last_ts'], str(r.get('ts') or ''))
        if r.get('domain'):
            d['domains'].add(str(r['domain']))
    rows = sorted(counts.values(), key=lambda x: x['count'], reverse=True)[:top]
    for x in rows:
        x['domains'] = sorted(x['domains'])
    return rows
