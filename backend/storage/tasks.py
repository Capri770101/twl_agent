"""独立封装版 tasks.py —— 生图任务管理（默认 mock 模式）。"""
from __future__ import annotations
import uuid
from typing import Any

from backend.storage.object_store import save_generated

# 内存存储任务状态
_tasks: dict[str, dict[str, Any]] = {}


async def create_image_task(prompt: str) -> str:
    """创建生图任务（mock 模式立即完成）。"""
    task_id = uuid.uuid4().hex[:16]
    # 生成一个最小占位 PNG，保证前端可以展示
    png_data = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
    result_url = save_generated(f'{task_id}.png', png_data)
    _tasks[task_id] = {
        'task_id': task_id,
        'status': 'done',
        'prompt': prompt,
        'result_url': result_url,
    }
    return task_id


async def get_image_task(task_id: str) -> dict[str, Any]:
    """获取任务状态。"""
    if task_id in _tasks:
        return _tasks[task_id]
    return {'task_id': task_id, 'status': 'not_found'}
