"""独立封装版 object_store.py。"""
from __future__ import annotations

from pathlib import Path

from backend.config import settings


def _safe_name(filename: str) -> str:
    """仅保留文件名（去掉任何路径成分），阻止目录穿越。"""
    name = Path(filename).name
    if not name or name in ('.', '..'):
        raise ValueError('invalid filename')
    return name


def save_generated(filename: str, data: bytes) -> str:
    name = _safe_name(filename)
    path = Path(settings.DB_PATH).parent / 'generated'
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(data)
    url = f'/generated/{name}'
    base = (getattr(settings, 'IMAGE_PUBLIC_BASE_URL', '') or '').strip()
    if base:
        url = f'{base.rstrip("/")}{url}'
    return url


def read_generated(filename: str) -> bytes | None:
    name = _safe_name(filename)
    path = Path(settings.DB_PATH).parent / 'generated' / name
    return path.read_bytes() if path.exists() else None
