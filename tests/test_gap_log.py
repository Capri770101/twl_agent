"""检索缺口日志测试：record_gap 落盘 + aggregate 聚合（用 tmp 路径，不污染仓库）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.knowledge import gap_log


def test_record_and_aggregate(tmp_path):
    p = tmp_path / "gaps.jsonl"
    gap_log.record_gap("怎么让花开久一点", "care", 0, 0.0, path=p)
    gap_log.record_gap("怎么让花开久一点", "care", 0, 0.0, path=p)
    gap_log.record_gap("什么花适合送领导", "all", 1, 0.05, path=p)
    gaps = gap_log.load_gaps(p)
    assert len(gaps) == 3
    rows = gap_log.aggregate(p, top=10)
    assert rows[0]["query"] == "怎么让花开久一点" and rows[0]["count"] == 2
    assert rows[0]["domains"] == ["care"]
    assert rows[1]["query"] == "什么花适合送领导"


def test_record_gap_skips_empty(tmp_path):
    p = tmp_path / "gaps.jsonl"
    gap_log.record_gap("", "flower", 0, 0.0, path=p)
    gap_log.record_gap("   ", "flower", 0, 0.0, path=p)
    assert gap_log.load_gaps(p) == []


def test_aggregate_missing_file(tmp_path):
    assert gap_log.aggregate(tmp_path / "nope.jsonl") == []
