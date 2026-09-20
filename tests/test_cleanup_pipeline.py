"""清理链管道回归门（2026-09-20 重构，对应外部 review 第 2 条）。

## 背景

原先 8 个清理/兜底函数是**硬编码散在 `run()` 末尾**的：顺序语义（谁必须在谁之前）
只存在于代码行的先后，改一处看不出后果；而其中几步是**强依赖**的 ——
比如 XSS 转义必须早于「内部泄漏清理」之后的模式匹配、独白兜底（整段替换）必须最后。

现在它们收进 `_CLEANUP_PIPELINE` 一张显式的阶段表，本文件锁住两件事：
1. **顺序**：调整必须显式改这里并说明理由（而不是悄悄挪一行）；
2. **每步都要有 `why`**：位置理由必须写下来。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.agent import _CLEANUP_PIPELINE, _run_cleanup_pipeline  # noqa: E402

EXPECTED_ORDER = [
    'strip_separator_lines',        # 删独立分隔线
    'strip_internal_leak',          # 隐私兜底（工具名 / 内部字段）
    'neutralize_dangerous_tags',    # XSS 兜底
    'ensure_card_summary',          # 要点兜底
    'ensure_non_empty_reply',       # 非空兜底
    'demo_trade_note',              # 体验版边界说明（条件）
    'finalize_image_claim',         # 生图声明兜底
    'finalize_reasoning_leak',      # 独白兜底（整段替换，必须最后）
]


def test_pipeline_order_is_locked():
    """🔒 清理链顺序被锁定。

    顺序**是语义**，不是排版：例如
    - `neutralize_dangerous_tags` 必须在 `strip_internal_leak` 之后（转义产物不该再被删）；
    - `finalize_reasoning_leak` 必须最后（它整段替换，放前面会覆盖后续步骤的产出）。

    若确实要调整顺序：先确认上面的依赖，再更新本断言并在提交信息里说明原因。
    """
    names = [step.name for step in _CLEANUP_PIPELINE]
    assert names == EXPECTED_ORDER, (
        f'清理链顺序被改动：\n  期望 {EXPECTED_ORDER}\n  实得 {names}\n'
        '调整前请先读各步 why（尤其含「必须」的那几步），再同步更新本断言。'
    )


def test_every_step_documents_position_reason():
    """每一步都必须写明「为什么在这个位置」——这是这次重构的核心产出。"""
    for step in _CLEANUP_PIPELINE:
        assert step.why and len(step.why) >= 8, f'{step.name} 缺少 why（位置理由）'
    # 两步强依赖必须有显式说明，避免后人误移
    whys = {step.name: step.why for step in _CLEANUP_PIPELINE}
    assert '必须' in whys['strip_internal_leak'] or '必须' in whys['neutralize_dangerous_tags']
    assert '必须' in whys['finalize_reasoning_leak']


def test_pipeline_handles_dirty_input_end_to_end():
    """脏输入经管道后：危险标签被转义、内部术语不泄漏、独立分隔线被删。"""
    class _UI:
        value = 'text'
    dirty = '你好<script>alert(1)</script>，我用 platform_db_query_entity 查了。\n---\n结果如上。'
    out = _run_cleanup_pipeline(dirty, ui=_UI(), data={}, tool_log=[])
    assert '<script>' not in out
    assert 'platform_db_query_entity' not in out
    assert '\n---\n' not in out
    assert out
