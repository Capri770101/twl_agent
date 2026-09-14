"""聚合检索缺口日志，列出 Top 高频未命中查询，指导补知识库（Tier 1.3）。

前置：缺口日志需已开启（`RAG_GAP_LOG_ENABLED=true`）并累积了流量。

用法：
  python scripts/report_gaps.py                 # Top 20
  python scripts/report_gaps.py --top 50
  python scripts/report_gaps.py --path data/eval/retrieval_gaps.jsonl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.knowledge.gap_log import aggregate, load_gaps  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description='检索缺口报表：Top 未命中查询')
    ap.add_argument('--top', type=int, default=20, help='展示前 N 类（默认 20）')
    ap.add_argument('--path', type=Path, default=None, help='缺口日志路径（默认 data/eval/retrieval_gaps.jsonl）')
    args = ap.parse_args()

    total = len(load_gaps(args.path))
    rows = aggregate(args.path, top=args.top)
    if not rows:
        print('暂无缺口记录。缺口日志默认关闭：将 RAG_GAP_LOG_ENABLED 置 true 并积累流量后再看。')
        return 0

    print(f'缺口记录 {total} 条，去重 {len(rows)} 类（Top {args.top}）：')
    print(f"{'次数':>4}  {'域':<12}查询")
    for r in rows:
        dom = ','.join(r['domains']) or '-'
        print(f"{r['count']:>4}  {dom:<12}{r['query']}")
    print('\n提示：高频缺口 = 知识库盲区，优先补对应域条目；补完顺带跑 scripts/eval_retrieval.py 锁基线。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
