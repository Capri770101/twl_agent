"""独立封装版 db_async.py。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from backend.storage import db


def dialect() -> str:
    return 'sqlite'


def _reset_engine() -> None:
    return None


def _dispose_engine() -> None:
    return None


async def _async_dispose_loop_engine(loop) -> None:
    return None


async def init_db_async() -> None:
    db.init_db()


@asynccontextmanager
async def transaction():
    with db.transaction() as conn:
        yield conn
