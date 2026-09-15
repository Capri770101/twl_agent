"""P1 健壮性回归门：工具参数兜底、卡片空文本兜底、生图任务时间戳工具。"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.agent import ReActAgent, _ensure_card_summary, _ensure_non_empty_reply, _strip_internal_leak
from agent.engine.ui_protocol import UIType
from backend.storage.tasks import _age_seconds


# ── _parse_tool_calls：模型参数 JSON 坏掉也不能崩 ──

class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, name: str, arguments: str, tid: str = "t1") -> None:
        self.function = _Fn(name, arguments)
        self.id = tid


class _Msg:
    def __init__(self, tool_calls) -> None:
        self.tool_calls = tool_calls


def test_parse_tool_calls_valid():
    calls = ReActAgent._parse_tool_calls(_Msg([_TC("retrieve_knowledge", '{"domain": "flower", "query": "玫瑰"}')]))
    assert len(calls) == 1
    assert calls[0]["name"] == "retrieve_knowledge"
    assert calls[0]["arguments"] == {"domain": "flower", "query": "玫瑰"}
    assert calls[0]["id"] == "t1"


def test_parse_tool_calls_survives_bad_json():
    # 截断/写坏的 JSON → 跳过该条，不抛异常
    calls = ReActAgent._parse_tool_calls(_Msg([_TC("generate_diy_plan", '{"recipient": "妈妈"')]))
    assert calls == []


def test_parse_tool_calls_skips_empty_name():
    calls = ReActAgent._parse_tool_calls(_Msg([_TC("", "{}")]))
    assert calls == []


def test_parse_tool_calls_non_dict_args():
    # 合法 JSON 但不是对象（如 123）→ 归一为 {}
    calls = ReActAgent._parse_tool_calls(_Msg([_TC("foo", "123")]))
    assert len(calls) == 1 and calls[0]["arguments"] == {}


def test_parse_tool_calls_no_tool_calls():
    assert ReActAgent._parse_tool_calls(_Msg(None)) == []


# ── _ensure_card_summary：卡片有、文字不能空 ──

def test_card_summary_fills_image_task():
    out = _ensure_card_summary("", UIType.IMAGE_TASK, {"task_id": "t"})
    assert out and "效果图" in out


def test_card_summary_fills_order():
    out = _ensure_card_summary("", UIType.ORDER_CARD, {"order_id": "O1"})
    assert out and "支付" in out


def test_card_summary_fills_greeting():
    out = _ensure_card_summary("   ", UIType.GREETING_CARD, {"image_url": "/g/1.png"})
    assert out and "贺卡" in out


def test_card_summary_keeps_long_reply():
    long_reply = "这是我已经足够长的一段说明文字，超过了三十个字符的阈值，不需要再追加任何要点。" * 2
    assert _ensure_card_summary(long_reply, UIType.IMAGE_TASK, {"task_id": "t"}) == long_reply


def test_card_summary_fills_plan_from_data():
    out = _ensure_card_summary("", UIType.PLAN_CARD, {"plans": [{"name": "生日花束", "price": 199}]})
    assert out and "生日花束" in out


def test_card_summary_ignores_text_ui():
    assert _ensure_card_summary("", UIType.TEXT, {}) == ""


# ── _ensure_non_empty_reply：清理链跑完仍为空 → 必须给出兜底（线上空回复缺陷）──

def test_non_empty_fills_blank_text():
    """纯文本被 _strip_internal_leak 清空后，必须补一句可读兜底（不能返回空）。"""
    out = _ensure_non_empty_reply("", UIType.TEXT)
    assert out.strip()
    assert "source_id" not in out


def test_non_empty_fills_whitespace_only():
    assert _ensure_non_empty_reply("   \n  ", UIType.TEXT).strip()


def test_non_empty_fills_blank_card():
    out = _ensure_non_empty_reply("", UIType.PLAN_CARD)
    assert out.strip() and "卡片" in out


def test_non_empty_keeps_existing_reply():
    assert _ensure_non_empty_reply("给你配了一束粉色系花束", UIType.TEXT) == "给你配了一束粉色系花束"


def test_non_empty_leak_then_fallback_chain():
    """模拟真实链路：正文只有一行原始 JSON → 脱敏后为空 → 兜底补上。"""
    raw = '{"source_id": "wxmini", "shop_id": "S1"}'
    stripped = _strip_internal_leak(raw)
    assert stripped.strip() == ""
    assert _ensure_non_empty_reply(stripped, UIType.TEXT).strip()


# ── _age_seconds：时间戳工具（判断 processing 是否超时）──

def test_age_seconds_none():
    assert _age_seconds(None) == 0.0


def test_age_seconds_recent():
    assert _age_seconds(datetime.now(UTC)) < 5


def test_age_seconds_old():
    assert _age_seconds(datetime.now(UTC) - timedelta(seconds=600)) > 500


def test_age_seconds_bad_input():
    assert _age_seconds("not-a-date") == 0.0
