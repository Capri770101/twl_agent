"""独立封装版 tasks.py —— 生图任务管理（支持 hy 大模型）。"""
from __future__ import annotations
import asyncio
import uuid
from typing import Any

from backend.config import settings
from backend.storage.db import transaction
from backend.storage.object_store import save_generated


def _save_task(task_id: str, status: str, prompt: str, user_id: str | None = None, result_url: str | None = None, error: str | None = None) -> None:
    """持久化任务状态，避免服务重启后丢失任务。"""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO image_tasks (task_id, user_id, status, prompt, result_url, error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, NOW(), NOW())
               ON CONFLICT (task_id) DO UPDATE SET
                   status=EXCLUDED.status, result_url=EXCLUDED.result_url,
                   error=EXCLUDED.error, updated_at=NOW()""",
            (task_id, user_id, status, prompt, result_url, error),
        )


def _update_task(task_id: str, status: str, result_url: str | None = None, error: str | None = None) -> None:
    with transaction() as conn:
        conn.execute(
            'UPDATE image_tasks SET status=?, result_url=?, error=?, updated_at=NOW() WHERE task_id=?',
            (status, result_url, error, task_id),
        )


def recover_incomplete_tasks() -> None:
    """服务重启后终止无法恢复的旧任务，避免永久 processing。"""
    with transaction() as conn:
        conn.execute(
            'UPDATE image_tasks SET status=?, error=?, updated_at=NOW() WHERE status=?',
            ('failed', '服务重启，未完成的生图任务已终止，请重新生成', 'processing'),
        )


def check_task_storage() -> None:
    """启动自检：尽早暴露部署方遗漏 image_tasks 表的问题。"""
    with transaction() as conn:
        conn.execute('SELECT task_id FROM image_tasks LIMIT 1').fetchone()


async def _generate_with_hy(prompt: str) -> str:
    """使用 hy 大模型生成图像，返回图像数据。"""
    import httpx
    
    if not settings.HY_API_KEY:
        raise RuntimeError('未配置 HY_API_KEY，无法使用 hy 大模型生成图像')
    
    headers = {
        'Authorization': f'Bearer {settings.HY_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    payload = {
        'model': settings.HY_IMAGE_MODEL,
        'prompt': prompt,
        'width': settings.IMAGE_WIDTH,
        'height': settings.IMAGE_HEIGHT
    }
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            settings.HY_BASE_URL,
            headers=headers,
            json=payload
        )
        response.raise_for_status()
        result = response.json()
        
        # 根据hy大模型的响应格式提取图像数据
        if 'data' in result and len(result['data']) > 0:
            image_url = result['data'][0].get('url', '')
            if image_url:
                # 下载图像数据
                img_response = await client.get(image_url)
                return img_response.content
        elif 'image' in result:
            import base64
            return base64.b64decode(result['image'])
    
    raise RuntimeError('hy 大模型图像生成失败：无法提取图像数据')


async def _generate_image_async(task_id: str, prompt: str) -> None:
    """异步生成图像并更新任务状态。"""
    try:
        png_data = await _generate_with_hy(prompt)
        result_url = save_generated(f'{task_id}.png', png_data)
        _update_task(task_id, 'done', result_url=result_url)
    except Exception as e:
        _update_task(task_id, 'failed', error=str(e))


async def create_image_task(prompt: str, user_id: str | None = None) -> str:
    """创建生图任务，支持 hy 大模型或 mock 模式。"""
    task_id = uuid.uuid4().hex[:16]
    _save_task(task_id, 'processing', prompt, user_id=user_id)
    
    if settings.HY_API_KEY and settings.IMAGE_PROVIDER == 'hy':
        # 使用 hy 大模型异步生成图像
        asyncio.create_task(_generate_image_async(task_id, prompt))
    else:
        # Mock 模式：生成占位图像
        png_data = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
        result_url = save_generated(f'{task_id}.png', png_data)
        _update_task(task_id, 'done', result_url=result_url)
    
    return task_id


async def get_image_task(task_id: str, user_id: str | None = None) -> dict[str, Any]:
    """获取任务状态。"""
    with transaction() as conn:
        row = conn.execute(
            'SELECT task_id, user_id, status, prompt, result_url, error, created_at, updated_at FROM image_tasks WHERE task_id=? AND (? IS NULL OR user_id=?)',
            (task_id, user_id, user_id),
        ).fetchone()
    if not row:
        return {'task_id': task_id, 'status': 'not_found'}
    return dict(row)
