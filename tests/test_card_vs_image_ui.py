"""「卡片优先于生图」回归测试。

背景（2026-09-16 线上实测）：模型出方案后会顺手调 generate_effect_image。
旧逻辑单轮扫描「最近一次产卡工具」，生图（排在方案之后）会顶掉方案卡 ——
用户只看到「效果图正在生成」，却拿不到花材与价格明细。
修法：`_derive_ui` 拆成两轮（卡片 → 生图），且卡片与生图并存时把 task_id 挂到卡片 data 上。
"""

from __future__ import annotations

import json

from agent.agent import ReActAgent, _extract_image_task
from agent.engine.ui_protocol import ToolCallRecord, UIType

_STAGE = None  # _derive_ui 不使用 stage/reply，传 None 即可


def _rec(name: str, payload: dict, status: str = 'ok', arguments: dict | None = None) -> ToolCallRecord:
    return ToolCallRecord(
        name=name,
        arguments=arguments or {},
        result=json.dumps(payload, ensure_ascii=False),
        status=status,
    )


def _derive(log: list[ToolCallRecord]) -> tuple[UIType, dict]:
    return ReActAgent._derive_ui(None, log, _STAGE, '')


# ── _derive_ui：卡片优先 ──────────────────────────────────────────────────

def test_plan_card_wins_over_image_task() -> None:
    """方案 + 生图并存 → 必须给方案卡（这是本次修复的核心）。"""
    log = [
        _rec('generate_diy_plan', {'plan_id': 'p1', 'name': '粉黛甜心'}),
        _rec('generate_effect_image', {'task_id': 't1', 'poll': '/tasks/t1'}),
    ]
    ui, data = _derive(log)
    assert ui == UIType.PLAN_CARD
    assert data['plans'][0]['plan_id'] == 'p1'


def test_image_only_still_works() -> None:
    """只有生图 → 仍按原行为出生图 UI（没有卡片可优先）。"""
    ui, data = _derive([_rec('generate_effect_image', {'task_id': 't1', 'poll': '/tasks/t1'})])
    assert ui == UIType.IMAGE_TASK
    assert data['task_id'] == 't1'


def test_image_with_result_url() -> None:
    ui, data = _derive([_rec('generate_effect_image', {'task_id': 't1', 'result_url': 'http://x/a.png'})])
    assert ui == UIType.IMAGE_TASK
    assert data['result_url'] == 'http://x/a.png'


def test_platform_products_win_over_image_task() -> None:
    """平台商品查询 + 生图 → 商品卡优先。"""
    log = [
        _rec(
            'platform_db_query_entity',
            {'ok': True, 'data': [{'name': '感恩母亲', 'price': 158}]},
            arguments={'entity': 'plan'},
        ),
        _rec('generate_effect_image', {'task_id': 't1'}),
    ]
    ui, data = _derive(log)
    assert ui == UIType.PLAN_CARD
    assert data['plans'][0]['name'] == '感恩母亲'


def test_greeting_card_wins_over_image_task() -> None:
    log = [
        _rec('render_greeting_card', {'image_url': 'http://x/card.png'}),
        _rec('generate_effect_image', {'task_id': 't1'}),
    ]
    ui, _ = _derive(log)
    assert ui == UIType.GREETING_CARD


def test_no_renderable_tool_falls_back_to_text() -> None:
    ui, data = _derive([_rec('save_memory', {'ok': True})])
    assert ui == UIType.TEXT
    assert data == {}


# ── _extract_image_task：必须有 task_id 才算真任务 ────────────────────────

def test_extract_image_task_requires_task_id() -> None:
    assert _extract_image_task([]) == {}
    # 调用了但没拿到 task_id（失败 / 工具报错）→ 不算
    assert _extract_image_task([_rec('generate_effect_image', {'error': 'boom'})]) == {}
    assert _extract_image_task([_rec('generate_effect_image', {'task_id': 't1'}, status='error')]) == {}


def test_extract_image_task_keeps_optional_fields() -> None:
    task = _extract_image_task([_rec('generate_effect_image', {'task_id': 't1', 'poll': '/p', 'result_url': 'u'})])
    assert task == {'task_id': 't1', 'poll': '/p', 'result_url': 'u'}
    # 没有 poll/result_url 时不塞空值
    assert _extract_image_task([_rec('generate_effect_image', {'task_id': 't1'})]) == {'task_id': 't1'}


def test_extract_image_task_takes_latest() -> None:
    log = [
        _rec('generate_effect_image', {'task_id': 'old'}),
        _rec('generate_effect_image', {'task_id': 'new'}),
    ]
    assert _extract_image_task(log)['task_id'] == 'new'
