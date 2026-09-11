"""独立封装版 tasks.py —— 生图任务管理（支持 hy 大模型）。"""
from __future__ import annotations
import asyncio
import ipaddress
import logging
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

from backend.config import settings
from backend.storage.db import transaction
from backend.storage.object_store import save_generated

logger = logging.getLogger('tasks')

_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 生图专用后台执行器：每个任务在**独立线程**里用**自带的事件循环**跑完整个生命周期。
#
# 为什么不能用 asyncio.create_task：生图工具经 ReAct 工具链调用，而工具链跑在
# agent/agent.py 的 arun() 里 `run_in_executor(None, lambda: asyncio.run(self.run(...)))`
# 所创建的**临时事件循环**中。run() 一返回，asyncio.run 便会关闭该循环并取消其上所有
# 挂起任务；而 CancelledError 继承自 BaseException，_generate_image_async 里的
# `except Exception` 抓不到它 —— 任务于是永远停在 processing（mock 分支是同步实现，
# 所以本地联调看不出来，只有真实 qwen/hy 生图才暴露）。
#
# 独立线程自带循环、与应用调用方生命周期解耦，从根本上规避该问题。
# max_workers 限制并发生图数（保护上游生图配额），超出的任务在队列中排队。
_IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix='flora-image')


def _run_image_task(task_id: str, prompt: str) -> None:
    """在后台线程内以独立事件循环执行生图协程，并兜底一切异常。

    Args:
        task_id: 生图任务 ID（已写入 image_tasks，状态 processing）。
        prompt: 生图提示词。
    """
    try:
        asyncio.run(_generate_image_async(task_id, prompt))
    except asyncio.CancelledError:
        # 显式兜底：CancelledError 不是 Exception 子类，下面那支抓不到它。
        logger.warning('[tasks] 生图任务被取消 task_id=%s', task_id)
        _update_task(task_id, 'failed', error='任务被取消')
    except Exception:
        logger.exception('[tasks] 生图后台执行异常 task_id=%s', task_id)
        _update_task(task_id, 'failed', error='生图后台执行异常')


def _assert_public_image_url(image_url: str) -> None:
    """校验生图结果 URL 为公网地址，阻止 SSRF（云元数据/内网/本机）。"""
    parsed = urlparse(image_url)
    if parsed.scheme not in ('http', 'https'):
        raise PermissionError('invalid image url scheme')
    hostname = parsed.hostname
    if not hostname:
        raise ValueError('image url missing hostname')
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError('image url hostname cannot be resolved') from exc
    for addr in addresses:
        ip = ipaddress.ip_address(addr[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise PermissionError('image url resolves to a private/internal address')


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
                _assert_public_image_url(image_url)
                # 下载图像数据（带大小上限）
                async with httpx.AsyncClient(timeout=120.0) as dl:
                    img_response = await dl.get(image_url)
                img_response.raise_for_status()
                if len(img_response.content) > _MAX_IMAGE_BYTES:
                    raise ValueError('generated image exceeds size limit')
                return img_response.content
        elif 'image' in result:
            import base64
            return base64.b64decode(result['image'])
    
    raise RuntimeError('hy 大模型图像生成失败：无法提取图像数据')


# 阿里云百炼（DashScope）Qwen-Image 原生生图地址
_QWEN_IMAGE_URL_DEFAULT = 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'


async def _generate_with_qwen(prompt: str) -> bytes:
    """使用阿里云百炼 Qwen-Image 生成图像，返回图像字节数据。

    为什么不用 OpenAI 兼容接口：实测 `POST /compatible-mode/v1/images/generations`
    对 `qwen-image-3.0` 返回 **404**，百炼的 Qwen-Image 只走原生
    `multimodal-generation` 接口。

    两个易错点：
    - 尺寸格式是 `"宽*高"`（如 `768*1024`），不是 OpenAI 的 `"宽x高"`；
    - 返回的图片是**带签名的 OSS 临时地址**（有 Expires），必须立刻下载，
      不能把该 URL 直接存起来给前端用（会过期 403）。
    """
    import httpx

    api_key = settings.IMAGE_API_KEY or settings.llm_api_key
    if not api_key:
        raise RuntimeError('未配置 IMAGE_API_KEY，无法使用百炼 Qwen-Image 生图')

    base_url = (settings.IMAGE_BASE_URL or _QWEN_IMAGE_URL_DEFAULT).strip() or _QWEN_IMAGE_URL_DEFAULT
    model = settings.IMAGE_MODEL or 'qwen-image-3.0'
    size = f'{settings.IMAGE_WIDTH}*{settings.IMAGE_HEIGHT}'

    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {
        'model': model,
        'input': {'messages': [{'role': 'user', 'content': [{'text': prompt}]}]},
        'parameters': {'size': size, 'n': 1},
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(base_url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()

    # 响应结构：output.choices[0].message.content[i].image = 图片地址
    image_url = ''
    try:
        choices = (result.get('output') or {}).get('choices') or []
        content = (choices[0].get('message') or {}).get('content') or []
        for item in content:
            if isinstance(item, dict) and item.get('image'):
                image_url = item['image']
                break
    except (IndexError, AttributeError, TypeError):
        image_url = ''
    if not image_url:
        raise RuntimeError(f'百炼生图失败：响应中未找到图片地址 -> {str(result)[:300]}')

    _assert_public_image_url(image_url)
    async with httpx.AsyncClient(timeout=120.0) as dl:
        img_response = await dl.get(image_url)
    img_response.raise_for_status()
    if len(img_response.content) > _MAX_IMAGE_BYTES:
        raise ValueError('generated image exceeds size limit')
    return img_response.content


async def _generate_image_async(task_id: str, prompt: str) -> None:
    """异步生成图像并更新任务状态（按 IMAGE_PROVIDER 选择实现）。"""
    try:
        if settings.IMAGE_PROVIDER == 'qwen':
            png_data = await _generate_with_qwen(prompt)
        else:
            png_data = await _generate_with_hy(prompt)
        result_url = save_generated(f'{task_id}.png', png_data)
        _update_task(task_id, 'done', result_url=result_url)
    except Exception as e:
        _update_task(task_id, 'failed', error=str(e))


async def create_image_task(prompt: str, user_id: str | None = None) -> str:
    """创建生图任务，支持 百炼 qwen / hy 大模型 / mock 模式。"""
    task_id = uuid.uuid4().hex[:16]
    _save_task(task_id, 'processing', prompt, user_id=user_id)

    if settings.IMAGE_PROVIDER == 'qwen' and (settings.IMAGE_API_KEY or settings.llm_api_key):
        # 阿里云百炼 Qwen-Image 异步生成（提交到独立后台线程，避免被调用方临时循环销毁）
        _IMAGE_EXECUTOR.submit(_run_image_task, task_id, prompt)
    elif settings.HY_API_KEY and settings.IMAGE_PROVIDER == 'hy':
        # 使用 hy 大模型异步生成图像
        _IMAGE_EXECUTOR.submit(_run_image_task, task_id, prompt)
    else:
        # Mock 模式：生成占位图像
        png_data = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
        result_url = save_generated(f'{task_id}.png', png_data)
        _update_task(task_id, 'done', result_url=result_url)
    
    return task_id


async def get_image_task(task_id: str, user_id: str | None = None) -> dict[str, Any]:
    """获取任务状态；提供 user_id 时严格按归属过滤，否则仅内部调用。"""
    with transaction() as conn:
        if user_id:
            row = conn.execute(
                'SELECT task_id, user_id, status, prompt, result_url, error, created_at, updated_at FROM image_tasks WHERE task_id=? AND user_id=?',
                (task_id, user_id),
            ).fetchone()
        else:
            row = conn.execute(
                'SELECT task_id, user_id, status, prompt, result_url, error, created_at, updated_at FROM image_tasks WHERE task_id=?',
                (task_id,),
            ).fetchone()
    if not row:
        return {'task_id': task_id, 'status': 'not_found'}
    return dict(row)
