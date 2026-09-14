"""proven 学习闭环测试：历史数据 → diy_plans → 检索召回增强。

设计原则：不连真实 PostgreSQL（与全测试套件一致）。通过 monkeypatch 把
backend.storage.diy.transaction 替换成内存 sqlite 假适配器，在『存储层边界』上
验证完整闭环——ETL 灌入 → list_proven_plans 排序 → query_knowledge 召回 proven。

覆盖：
- 历史导入 + order_count 降序排序
- 导入幂等（同记录重跑 = 同一 plan_id，order_count 取绝对值不累加）
- 畸形记录拒绝（ValueError）
- mark_diy_plan_ordered：空 id 安全跳过 + 真实 id 正确 +1
- proven 域经 query_knowledge('all') 被相似问句召回（端到端）
- 离线 ETL 脚本 _load_records 解析（JSON 数组 / JSONL）
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.knowledge import store as knowledge_store
from backend.storage import diy as diy_storage

_DDL = """CREATE TABLE IF NOT EXISTS diy_plans (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, fingerprint TEXT NOT NULL, name TEXT NOT NULL,
    requirement TEXT, recipient TEXT, occasion TEXT, style TEXT, budget REAL,
    color_scheme TEXT, flowers TEXT, packaging TEXT, meaning TEXT, diy_steps TEXT,
    care_tips TEXT, card_message TEXT, card_image_url TEXT, budget_breakdown TEXT,
    effect_image_url TEXT, difficulty TEXT, est_time INTEGER, shelf_life TEXT,
    suitable_for TEXT, caution TEXT, mood_tags TEXT, status TEXT NOT NULL DEFAULT 'confirmed',
    order_count INTEGER NOT NULL DEFAULT 0, source_user_id TEXT, created_at TEXT, confirmed_at TEXT)"""


class _FakeAdapter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def closed(self) -> bool:
        return False

    def execute(self, sql: str, params=None):
        # sqlite 原生支持 ? 占位符；psycopg 的 %s 转换不影响此处
        return self._conn.execute(sql, params or ())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


@pytest.fixture
def fake_diy_db(monkeypatch):
    """用内存 sqlite 替换 diy.transaction，单次测试内连接复用（数据可跨调用保留）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    conn.commit()

    @contextmanager
    def _tx():
        try:
            yield _FakeAdapter(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(diy_storage, "transaction", _tx)
    yield conn
    conn.close()


def test_import_proven_plan_and_ordering(fake_diy_db):
    pid1 = diy_storage.import_proven_plan({
        "occasion": "求婚", "style": "韩式", "flowers": ["玫瑰", "蝴蝶兰"],
        "color_scheme": ["香槟", "白"], "budget": 300, "meaning": "一生承诺",
        "success_count": 12,
    })
    pid2 = diy_storage.import_proven_plan({
        "occasion": "探病", "style": "日式", "flowers": ["百合", "小雏菊"],
        "budget": 150, "meaning": "早日康复", "success_count": 3,
    })
    assert pid1.startswith("HIST_") and pid2.startswith("HIST_")
    plans = diy_storage.list_proven_plans()
    assert len(plans) == 2
    # 高成交排前
    assert plans[0]["occasion"] == "求婚" and plans[0]["order_count"] == 12
    assert plans[1]["occasion"] == "探病"


def test_import_proven_plan_idempotent(fake_diy_db):
    rec = {"occasion": "生日", "style": "韩式", "flowers": ["玫瑰"], "success_count": 5}
    pid_a = diy_storage.import_proven_plan(rec)
    # 重跑同一条记录（即便 success_count 不同），plan_id 不变，order_count 取绝对值不累加
    pid_b = diy_storage.import_proven_plan(dict(rec, success_count=999))
    assert pid_a == pid_b
    plans = diy_storage.list_proven_plans()
    assert len(plans) == 1
    assert plans[0]["order_count"] == 999


def test_import_proven_plan_rejects_malformed(fake_diy_db):
    with pytest.raises(ValueError):
        diy_storage.import_proven_plan({"foo": "bar"})  # 缺 occasion/style/flowers
    with pytest.raises(ValueError):
        diy_storage.import_proven_plan("not a dict")


def test_mark_ordered_noop_on_empty():
    # 空 id 应在触碰 DB 前返回，不报错
    asyncio.run(diy_storage.mark_diy_plan_ordered(""))


def test_mark_ordered_increments(fake_diy_db):
    pid = diy_storage.import_proven_plan({"occasion": "生日", "flowers": ["玫瑰"], "success_count": 1})
    asyncio.run(diy_storage.mark_diy_plan_ordered(pid))
    plans = diy_storage.list_proven_plans()
    assert plans[0]["order_count"] == 2


def test_proven_surfaces_in_retrieval(fake_diy_db):
    # 灌入高成交求婚方案 → query_knowledge('all','求婚花束') 应召回 proven 条目
    diy_storage.import_proven_plan({
        "occasion": "求婚", "style": "韩式", "flowers": ["玫瑰", "蝴蝶兰"],
        "meaning": "一生承诺", "budget": 300, "success_count": 20,
    })
    res = knowledge_store.query_knowledge("all", "求婚花束")
    proven_hits = [r for r in res["results"] if r.get("_domain") == "proven"]
    assert proven_hits, "proven 域未被召回"
    assert any(h.get("occasion") == "求婚" for h in proven_hits)
    assert all(h["_score"] > 0 for h in proven_hits)


def _load_script():
    path = ROOT / "scripts" / "learn_from_history.py"
    spec = importlib.util.spec_from_file_location("learn_history_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_etl_load_records_json_array(tmp_path):
    p = tmp_path / "h.json"
    p.write_text(json.dumps([{"occasion": "生日", "flowers": ["玫瑰"]}, {"occasion": "探病"}]), encoding="utf-8")
    mod = _load_script()
    assert len(mod._load_records(p)) == 2


def test_etl_load_records_jsonl(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"occasion": "生日"}\n{"occasion": "探病"}\n', encoding="utf-8")
    mod = _load_script()
    assert len(mod._load_records(p)) == 2
