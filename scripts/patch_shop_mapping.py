"""K-3 修复：补齐 active 映射中 shop 实体缺失的 5 个字段（幂等、可重跑）。

背景
----
运营/前端反馈店铺基础信息不全（营业时间/状态/配送费/起送价等取不到）。
根因：active 映射的 shop 实体只映射了 8 个字段，缺失以下 5 个：
    business_hours, status, delivery_time, delivery_fee, min_order_price
平台 flower_shop.shops 表这 5 列确实存在（已用 discover 确认），只是未在映射白名单内。

做法
----
复用 mapping_store 的标准流程，避免手搓 SQL 主键冲突：
1. 取所有 status='active' 的映射；
2. 对 shop 实体 selected.columns 补齐缺失 canonical 字段（canonical=actual，与现有命名一致）；
3. 对 delivery_fee / min_order_price（int 分）补 cents_to_yuan 转换，与 plan.price 保持一致；
4. 用 save_mapping_draft 生成新版本（新 uuid + version+1）+ update_mapping_status 激活，
   旧 active 自动 revoked，审计完整、可回滚（旧版本行仍保留）。

运行：docker exec -i -e PYTHONPATH=/app flora-agent python - < scripts/patch_shop_mapping.py
"""
from __future__ import annotations

import json

from backend.storage.db import transaction
from backend.data_gateway.mapping_store import save_mapping_draft, update_mapping_status

# canonical -> actual 列名（与现有映射命名约定一致：canonical 直接用平台真实列名）
_SHOP_MISSING_COLUMNS = {
    "business_hours": "business_hours",
    "status": "status",
    "delivery_time": "delivery_time",
    "delivery_fee": "delivery_fee",
    "min_order_price": "min_order_price",
}
# 金额类字段（平台以「分」存储），需 cents_to_yuan，与 plan.price 保持一致
_AMOUNT_TRANSFORMS = {
    "delivery_fee": "cents_to_yuan",
    "min_order_price": "cents_to_yuan",
}


def _patch_draft(draft: dict) -> tuple[dict, bool, list[str]]:
    """就地修补 draft_json 的 shop 实体。返回 (新draft, 是否变更, 新增项列表)。"""
    entities = draft.setdefault("entities", {})
    shop = entities.setdefault("shop", {})
    selected = shop.setdefault("selected", {})
    columns = dict(selected.get("columns") or {})
    transforms = dict(selected.get("transforms") or {})

    added: list[str] = []
    for canonical, actual in _SHOP_MISSING_COLUMNS.items():
        if canonical not in columns:
            columns[canonical] = actual
            added.append(canonical)
    for canonical, spec in _AMOUNT_TRANSFORMS.items():
        if canonical in columns and transforms.get(canonical) != spec:
            transforms[canonical] = spec
            added.append(f"{canonical}:{spec}")

    if not added:
        return draft, False, []
    selected["columns"] = columns
    selected["transforms"] = transforms
    shop["selected"] = selected
    entities["shop"] = shop
    draft["entities"] = entities
    return draft, True, added


def patch_all() -> list[dict]:
    """对全部 active 映射执行补齐。返回每条处理结果。"""
    results: list[dict] = []
    with transaction() as conn:
        rows = conn.execute(
            "SELECT id, source_id, schema_name, schema_fingerprint, version, draft_json "
            "FROM mapping_drafts WHERE status='active' ORDER BY source_id, version DESC"
        ).fetchall()
        for row in rows:
            draft = dict(row["draft_json"])
            new_draft, changed, added = _patch_draft(draft)
            if not changed:
                results.append({"source_id": row["source_id"], "changed": False, "added": []})
                continue
            profile = {
                "source_id": row["source_id"],
                "schema": row["schema_name"],
                "schema_fingerprint": row["schema_fingerprint"],
            }
            saved = save_mapping_draft(profile, new_draft, actor="patch_shop_mapping")
            update_mapping_status(saved["id"], "active", "patch_shop_mapping")
            results.append({
                "source_id": row["source_id"],
                "new_id": saved["id"],
                "version": saved["version"],
                "changed": True,
                "added": added,
            })
    return results


if __name__ == "__main__":
    print(json.dumps(patch_all(), ensure_ascii=False, indent=2))
