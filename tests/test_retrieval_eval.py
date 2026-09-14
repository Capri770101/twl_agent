"""检索黄金集自动回归门：hit@5 / MRR 不得低于基线。

这是「自动学习」的质量闸——任何触碰知识库 / 检索阈值 / 同义词 / embedding 的改动，
跑 pytest 就会在这里被拦住。纯本地计算，不连 DB / LLM / 网络。
基线更新：`python scripts/eval_retrieval.py --update-baseline`（确认改动确实变好后再做）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN = ROOT / "tests" / "eval" / "golden_queries.json"
BASELINE = ROOT / "tests" / "eval" / "retrieval_baseline.json"


def _load_eval_module():
    """按路径加载 scripts/eval_retrieval.py（scripts 不在包内，用 importlib）。"""
    path = ROOT / "scripts" / "eval_retrieval.py"
    spec = importlib.util.spec_from_file_location("eval_retrieval_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_golden_set_wellformed():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = golden.get("cases", [])
    assert len(cases) >= 20, "黄金集用例过少"
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "用例 id 重复"
    for c in cases:
        assert c.get("query") and c.get("domain") and c.get("expect_any"), f"用例字段缺失：{c.get('id')}"


def test_golden_retrieval_meets_baseline():
    if not BASELINE.exists():
        pytest.skip("基线未生成：先跑 scripts/eval_retrieval.py --update-baseline")
    mod = _load_eval_module()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cur = mod.evaluate(golden)
    ref = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert cur["hit@5"] >= ref["hit@5"], f"hit@5 回归：{cur['hit@5']} < 基线 {ref['hit@5']}"
    assert cur["mrr"] >= ref["mrr"] - 1e-9, f"MRR 回归：{cur['mrr']} < 基线 {ref['mrr']}"
