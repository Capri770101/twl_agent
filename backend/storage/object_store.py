"""独立封装版 object_store.py。"""
from __future__ import annotations

from pathlib import Path

from backend.config import settings


def save_generated(filename: str, data: bytes) -> str:
    path = Path(settings.DB_PATH).parent / 'generated'
    path.mkdir(parents=True, exist_ok=True)
    (path / filename).write_bytes(data)
    url = f'/generated/{filename}'
    base = (getattr(settings, 'IMAGE_PUBLIC_BASE_URL', '') or '').strip()
    if base:
        url = f'{base.rstrip("/")}{url}'
    return url


def read_generated(filename: str) -> bytes | None:
    path = Path(settings.DB_PATH).parent / 'generated' / filename
    return path.read_bytes() if path.exists() else None
