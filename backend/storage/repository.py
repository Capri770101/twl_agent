"""简化版 repository.py —— 数据访问层，用于独立部署。"""
from __future__ import annotations
import logging
from typing import Any

from backend.storage.db import transaction

logger = logging.getLogger('repository')


class Repository:
    """数据仓库：直接读写 PostgreSQL。"""

    async def search_plans(self, keyword: str = '', limit: int = 10, **kwargs) -> list[dict[str, Any]]:
        with transaction() as conn:
            if keyword:
                rows = conn.execute(
                    'SELECT * FROM plans WHERE name LIKE ? OR desc LIKE ? OR tags LIKE ? LIMIT ?',
                    (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%', limit)
                ).fetchall()
            else:
                rows = conn.execute('SELECT * FROM plans LIMIT ?', (limit,)).fetchall()
        return [dict(r) for r in rows]

    async def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with transaction() as conn:
            row = conn.execute('SELECT * FROM plans WHERE id = ?', (plan_id,)).fetchone()
        return dict(row) if row else None

    async def search_shops(self, keyword: str = '', lat: float | None = None, lng: float | None = None, limit: int = 10, **kwargs) -> list[dict[str, Any]]:
        with transaction() as conn:
            if keyword:
                rows = conn.execute(
                    'SELECT * FROM shops WHERE name LIKE ? OR address LIKE ? LIMIT ?',
                    (f'%{keyword}%', f'%{keyword}%', limit)
                ).fetchall()
            else:
                rows = conn.execute('SELECT * FROM shops LIMIT ?', (limit,)).fetchall()
        return [dict(r) for r in rows]

    async def list_shops(self, plan_obj=None, location=None, requirement=None, limit: int = 10) -> list[dict[str, Any]]:
        with transaction() as conn:
            rows = conn.execute('SELECT * FROM shops ORDER BY rating DESC LIMIT ?', (limit,)).fetchall()
        return [dict(r) for r in rows]

    async def get_shop(self, shop_id: str) -> dict[str, Any] | None:
        with transaction() as conn:
            row = conn.execute('SELECT * FROM shops WHERE id = ?', (shop_id,)).fetchone()
        return dict(row) if row else None

    async def get_shop_plans(self, shop_id: str) -> list[dict[str, Any]]:
        with transaction() as conn:
            rows = conn.execute(
                'SELECT p.* FROM plans p JOIN shop_plans sp ON p.id=sp.plan_id WHERE sp.shop_id = ? AND sp.status = ?',
                (shop_id, 'on')
            ).fetchall()
        return [dict(r) for r in rows]

    async def get_shop_inventory(self, shop_id: str, plan_id: str) -> dict[str, Any] | None:
        with transaction() as conn:
            row = conn.execute(
                'SELECT * FROM shop_plans WHERE shop_id = ? AND plan_id = ?',
                (shop_id, plan_id)
            ).fetchone()
        return dict(row) if row else None


# 全局实例
repo = Repository()
