"""独立封装版 notify.py。"""
from __future__ import annotations

from typing import Any


T_ANNOUNCE = 'announcement'
CH_INBOX = 'inbox'


async def try_create(user_id: str, ntype: str, title: str, body: str = '', ref_type: str = '', ref_id: str = '', push_channel: str = CH_INBOX) -> dict[str, Any] | None:
    return None


async def broadcast(title: str, body: str = '', ntype: str = T_ANNOUNCE, ref_type: str = '', ref_id: str = '', user_ids: list[str] | None = None) -> int:
    return 0
