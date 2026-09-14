"""离线历史学习 ETL：把导出的历史订单 / 对话成交数据灌入 diy_plans（proven 域）。

这是智能体「通过历史数据自我学习」的离线入口。它把历史成交记录转成 proven 方案，
使 proven 域在相似需求下召回高成交组合。安全边界（红线）：纯 curated ETL，
不调 LLM、不改代码 / 工具 / 提示词（L4 自改代码为禁止项）。

用法：
  python scripts/learn_from_history.py --source data/history_orders.json
  python scripts/learn_from_history.py --source data/history.jsonl --dry-run
  python scripts/learn_from_history.py --source data/history_orders.json --limit 50

输入格式（JSON 数组 或 JSONL，每条一条记录）：
  {
    "recipient": "妈妈", "occasion": "生日", "style": "韩式",
    "budget": 300, "flowers": ["玫瑰", "百合"], "color_scheme": ["粉", "白"],
    "packaging": "雾面纸", "meaning": "生日快乐", "success_count": 5
  }

幂等：同一条记录（核心字段相同）生成同一 plan_id（HIST_ 前缀），重跑不重复累加。
order_count 取 success_count 绝对值：重跑同一导出结果幂等。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从仓库根运行（不依赖安装为包）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.storage.diy import import_proven_plan


def _load_records(path: Path) -> list[dict]:
    """读取 JSON 数组或 JSONL；返回记录列表。"""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _preview(rec: dict) -> dict:
    return {
        "plan_id": rec.get("plan_id") or "(auto)",
        "occasion": rec.get("occasion"),
        "style": rec.get("style"),
        "flowers": rec.get("flowers"),
        "budget": rec.get("budget"),
        "success_count": rec.get("success_count", 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="历史数据 → proven 域 ETL")
    ap.add_argument("--source", required=True, type=Path, help="历史数据文件（.json 数组或 .jsonl）")
    ap.add_argument("--dry-run", action="store_true", help="只打印规范化后的记录，不写库")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0=全部）")
    args = ap.parse_args()

    if not args.source.exists():
        print(f"[ERR] 文件不存在: {args.source}", file=sys.stderr)
        return 2

    records = _load_records(args.source)
    if args.limit:
        records = records[: args.limit]

    imported = 0
    skipped = 0
    for i, rec in enumerate(records, 1):
        if not isinstance(rec, dict):
            print(f"[SKIP] #{i} 非对象记录")
            skipped += 1
            continue
        if not (rec.get("occasion") or rec.get("style") or rec.get("flowers")):
            print(f"[SKIP] #{i} 缺少 occasion/style/flowers 任一：{rec.get('name', '')}")
            skipped += 1
            continue
        if args.dry_run:
            print(f"[DRY] #{i} -> {json.dumps(_preview(rec), ensure_ascii=False)}")
            imported += 1
            continue
        try:
            import_proven_plan(rec)
            imported += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR] #{i} 写入失败: {exc}", file=sys.stderr)
            skipped += 1

    print(
        f"\n完成：{'拟导入' if args.dry_run else '已导入'} {imported} 条，跳过 {skipped} 条。"
        f"{' (dry-run)' if args.dry_run else ''}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
