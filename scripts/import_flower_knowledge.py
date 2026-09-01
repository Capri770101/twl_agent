"""导入开源花卉知识源到本地 knowledge/sources 目录。

当前脚本是标准化入口，先把外部数据源转换成统一结构，再由人工或后续脚本并入
agent/knowledge/*.json。
"""
from __future__ import annotations

import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = BASE_DIR / "agent" / "knowledge"
SOURCES_DIR = KNOWLEDGE_DIR / "sources"


def _read_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_flower_record(item: dict, source: str) -> dict:
    """把外部来源的记录统一成知识库可读结构。"""
    name = item.get("name") or item.get("flower_name") or item.get("cnflower") or ""
    aliases = item.get("aliases") or item.get("aka") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    record = {
        "id": item.get("id") or f"EXT_{source.upper()}_{name}",
        "name": name,
        "aliases": aliases,
        "tags": tags,
        "source": source,
    }
    for key in (
        "flower_language",
        "care",
        "season",
        "colors",
        "price_tier",
        "pairing_notes",
        "scene",
        "bloom_months",
        "maintenance_notes",
        "description",
    ):
        if key in item and item[key] not in (None, "", [], {}):
            record[key] = item[key]
    return record


def main() -> None:
    manifest_path = KNOWLEDGE_DIR / "knowledge_manifest.json"
    manifest = _read_json(manifest_path)
    _write_json(SOURCES_DIR / "manifest.copy.json", manifest)
    print(f"knowledge manifest copied to {SOURCES_DIR / 'manifest.copy.json'}")


if __name__ == "__main__":
    main()
