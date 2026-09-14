"""答非所问修复回归门：`is_affirmative` 的否定式判定。

背景：`_AFFIRMATIVE` 含 '行' / '是' / '好' / '要' 这类**单字**，任何含它们的否定句
（「方案不行」「不是这个」「不好看」「不想要」）都会被误判为**肯定**，进而误确认方案 /
误触发生图 —— 表现为「答非所问」。本测试锁住修复，防止回退。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.agent import is_affirmative


@pytest.mark.parametrize(
    "msg",
    [
        "这个方案不行",
        "方案不行",
        "不是这个",
        "不好看",
        "不想要了",
        "不同意",
        "不合适",
        "不满意",
        "不喜欢这个",
        "不考虑了",
        "不需要",
        "不用了",
        "算了",
        "别生成",
    ],
)
def test_negative_messages_are_not_affirmative(msg):
    assert is_affirmative(msg) is False, f"「{msg}」被误判为肯定"


@pytest.mark.parametrize("msg", ["好的", "可以", "确认一下", "行了", "要这个", "同意", "生成吧"])
def test_positive_messages_are_affirmative(msg):
    assert is_affirmative(msg) is True, f"「{msg}」应判为肯定"


def test_empty_is_not_affirmative():
    assert is_affirmative("") is False
