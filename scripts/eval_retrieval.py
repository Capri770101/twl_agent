"""检索质量评测：黄金集 → hit@k / MRR + 基线回归。

为什么需要它：知识库/检索的任何改动（扩域、改阈值、加同义词、换 embedding）都必须
先回答「到底变好没」。本脚本把「靠手感」变成「靠数字」，是后续「自动学习/自动调参」
的质量闸地基——没有评测，任何自动优化都是盲开。

用法：
  python scripts/eval_retrieval.py                  # 跑评测并与基线对比，回归则退出码 1
  python scripts/eval_retrieval.py --update-baseline # 用当前结果刷新基线（改动确认变好后再做）
  python scripts/eval_retrieval.py --verbose         # 打印未命中明细
  python scripts/eval_retrieval.py --fail-under 0.9  # 直接指定 hit@5 门槛

指标：
  hit@1 / hit@3 / hit@5：期望条目是否落在前 k（k 见评测集，默认 5）。
  MRR：首个命中名次的倒数均值（越接近 1 越好）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.knowledge.store import query_knowledge  # noqa: E402

GOLDEN = ROOT / 'tests' / 'eval' / 'golden_queries.json'
BASELINE = ROOT / 'tests' / 'eval' / 'retrieval_baseline.json'
TOLERANCE = 0.0


def _identity_text(entry: dict, fields: list[str]) -> str:
    """只取「身份字段」拼文本做匹配，避免命中别条目的长文本（如 pairing_notes）造成假阳。"""
    parts: list[str] = []
    for f in fields:
        v = entry.get(f)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, (int, float)):
            parts.append(str(v))
    return ' '.join(parts)


def evaluate(golden: dict) -> dict:
    """跑一遍黄金集，返回指标 + 逐条明细。"""
    k = int(golden.get('k', 5))
    fields = golden.get('identity_fields', ['id', 'name', 'title'])
    cases = golden.get('cases', [])
    rows: list[dict] = []
    for c in cases:
        res = query_knowledge(c.get('domain', 'all'), c.get('query', ''))
        results = res.get('results', [])
        top = results[:k]
        expect = c.get('expect_any') or []
        rank = None
        for i, entry in enumerate(top, 1):
            ident = _identity_text(entry, fields)
            if any(sub and sub in ident for sub in expect):
                rank = i
                break
        rows.append({'id': c.get('id'), 'domain': c.get('domain'), 'query': c.get('query'), 'rank': rank, 'n': len(results)})

    n = len(rows) or 1

    def rate(pred) -> float:
        return round(sum(1 for r in rows if pred(r)) / n, 4)

    hit1 = rate(lambda r: r['rank'] == 1)
    hit3 = rate(lambda r: r['rank'] is not None and r['rank'] <= 3)
    hit5 = rate(lambda r: r['rank'] is not None and r['rank'] <= 5)
    mrr = round(sum((1.0 / r['rank']) for r in rows if r['rank']) / n, 4)

    per_domain: dict[str, list[dict]] = {}
    for r in rows:
        per_domain.setdefault(r['domain'], []).append(r)
    domain_hit5 = {
        d: round(sum(1 for r in v if r['rank'] is not None and r['rank'] <= 5) / len(v), 4)
        for d, v in per_domain.items()
    }
    return {'k': k, 'n': len(rows), 'hit@1': hit1, 'hit@3': hit3, 'hit@5': hit5, 'mrr': mrr, 'domain_hit@5': domain_hit5, 'rows': rows}


def main() -> int:
    ap = argparse.ArgumentParser(description='检索黄金集评测 + 基线回归')
    ap.add_argument('--update-baseline', action='store_true', help='用当前结果刷新基线')
    ap.add_argument('--verbose', action='store_true', help='打印未命中明细')
    ap.add_argument('--fail-under', type=float, default=None, help='hit@5 低于此值即失败（默认用基线值）')
    args = ap.parse_args()

    if not GOLDEN.exists():
        print(f'[ERR] 评测集不存在：{GOLDEN}', file=sys.stderr)
        return 2
    golden = json.loads(GOLDEN.read_text(encoding='utf-8'))
    cur = evaluate(golden)

    print(f"用例 {cur['n']} 条 | hit@1={cur['hit@1']}  hit@3={cur['hit@3']}  hit@5={cur['hit@5']}  MRR={cur['mrr']}")
    print('按域 hit@5:', json.dumps(cur['domain_hit@5'], ensure_ascii=False))
    misses = [r for r in cur['rows'] if r['rank'] is None]
    if args.verbose or misses:
        for r in misses:
            print(f"  [MISS] {r['id']}  q=«{r['query']}»  domain={r['domain']}  候选={r['n']}条")

    snapshot = {k: cur[k] for k in ('k', 'n', 'hit@1', 'hit@3', 'hit@5', 'mrr', 'domain_hit@5')}
    if args.update_baseline:
        BASELINE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'基线已更新 → {BASELINE}')
        return 0

    ref = json.loads(BASELINE.read_text(encoding='utf-8')) if BASELINE.exists() else None
    floor = args.fail_under if args.fail_under is not None else (ref or {}).get('hit@5')
    ok = True
    if floor is not None and cur['hit@5'] + TOLERANCE < floor:
        print(f"[FAIL] hit@5={cur['hit@5']} 低于门槛/基线 {floor}")
        ok = False
    if ref and cur['mrr'] + TOLERANCE < ref.get('mrr', 0):
        print(f"[FAIL] MRR={cur['mrr']} 低于基线 {ref.get('mrr')}")
        ok = False
    print('[OK] 检索质量不低于基线' if ok else '[FAIL] 检索质量发生回归')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
