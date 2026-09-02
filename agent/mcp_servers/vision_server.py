"""vision MCP server —— 用智谱 GLM-4V（视觉模型）把图片转成文字描述。

用途：为「无视觉能力」的模型提供读图能力（工具名 mcp__vision__describe_image）。
支持本地文件路径 / http(s) URL / data URI；依赖项目 .env 中的 ZHIPU_API_KEY。
启动：python agent/mcp_servers/vision_server.py （stdio 协议，供 dsh-mcp-client 拉起）
"""
from __future__ import annotations

import base64
import ipaddress
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import httpx
from backend.config import settings
from mcp.server.fastmcp import FastMCP

mcp = FastMCP('vision')
_MIME = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png', 'webp': 'webp', 'gif': 'gif'}

def _to_data_url(image: str) -> str:
    """本地路径 / URL / data URI → data URI。"""
    if image.startswith('data:'):
        return image
    if image.startswith(('http://', 'https://')):
        from urllib.parse import urlparse
        hostname = urlparse(image).hostname
        if not hostname:
            raise ValueError('图片 URL 缺少主机名')
        try:
            addresses = socket.getaddrinfo(hostname, None)
            if any(ipaddress.ip_address(item[4][0]).is_private for item in addresses):
                raise PermissionError('禁止访问内网图片地址')
        except socket.gaierror as exc:
            raise ValueError('图片 URL 主机名无法解析') from exc
        resp = httpx.get(image, timeout=30)
        resp.raise_for_status()
        if len(resp.content) > settings.VISION_MAX_IMAGE_BYTES:
            raise ValueError('图片超过允许大小')
        ext = image.rsplit('.', 1)[-1].lower() if '.' in image else 'jpeg'
        mime = _MIME.get(ext, 'jpeg')
        return f'data:image/{mime};base64,' + base64.b64encode(resp.content).decode()
    root = Path(settings.VISION_ALLOWED_ROOT).resolve()
    p = Path(image).resolve()
    if root not in p.parents and p != root:
        raise PermissionError('图片路径不在允许的目录内')
    if not p.is_file():
        raise FileNotFoundError(f'图片文件不存在: {image}')
    if p.stat().st_size > settings.VISION_MAX_IMAGE_BYTES:
        raise ValueError('图片超过允许大小')
    mime = _MIME.get(p.suffix.lstrip('.').lower(), 'jpeg')
    return f'data:image/{mime};base64,' + base64.b64encode(p.read_bytes()).decode()

@mcp.tool()
def describe_image(image: str, question: str='用中文描述这张图片（主体/颜色/风格/细节），50字内') -> str:
    """读取一张图片并返回文字描述（OCR 与视觉理解通用）。

    Args:
        image: 图片的本地绝对路径、http(s) URL 或 data URI。
        question: 可选自定义问题，如「这是什么花？有什么寓意？」。
    """
    key = settings.ZHIPU_API_KEY
    if not key:
        return '错误：未配置 ZHIPU_API_KEY，无法调用视觉模型（请在 .env 设置后重启）'
    try:
        data_url = _to_data_url(image)
    except Exception as exc:
        return f'错误：读取图片失败 - {exc}'
    try:
        resp = httpx.post('https://open.bigmodel.cn/api/paas/v4/chat/completions', headers={'Authorization': f'Bearer {key}'}, json={'model': os.environ.get('GLM_VISION_MODEL', 'glm-4v-flash'), 'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': data_url}}, {'type': 'text', 'text': question}]}]}, timeout=90)
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content']
    except Exception as exc:
        return f'错误：视觉模型调用失败 - {exc}'
if __name__ == '__main__':
    mcp.run()
