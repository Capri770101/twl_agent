"""gateway.py —— 通用只读数据接入网关。

提供智能体可直接使用的文件读取与数据库发现/查询能力，默认只读、受限、可审计。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from backend.storage.db import get_conn
from backend.storage.db_async import dialect as _db_dialect

logger = logging.getLogger('data_gateway')

_ALLOWED_ROOT = Path.cwd().resolve()
_MAX_READ_BYTES = 200_000
_MAX_SAMPLE_ROWS = 20
_MAX_QUERY_ROWS = 100
_DISALLOWED_SQL = re.compile(r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|VACUUM|PRAGMA|ATTACH|DETACH|TRUNCATE|REINDEX|GRANT|REVOKE)\b', re.I)
_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _validate_table(table: str) -> str:
    if not _IDENTIFIER.match(table or ''):
        raise PermissionError('invalid table name')
    return table


def _resolve_safe_path(path: str) -> Path:
    p = (_ALLOWED_ROOT / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if _ALLOWED_ROOT not in p.parents and p != _ALLOWED_ROOT:
        raise PermissionError(f'path out of allowed root: {path}')
    return p


def list_files(path: str = '.', exclude: list[str] | None = None, depth: int = 2) -> list[dict[str, Any]]:
    base = _resolve_safe_path(path)
    if not base.exists() or not base.is_dir():
        raise FileNotFoundError(path)
    exclude = exclude or []
    out: list[dict[str, Any]] = []

    def walk(cur: Path, d: int) -> None:
        if d < 0:
            return
        for child in sorted(cur.iterdir(), key=lambda x: x.name.lower()):
            if any(part in exclude for part in child.parts):
                continue
            rel = child.relative_to(_ALLOWED_ROOT).as_posix()
            out.append({'name': child.name, 'path': rel, 'type': 'dir' if child.is_dir() else 'file'})
            if child.is_dir() and d > 0:
                walk(child, d - 1)

    walk(base, depth)
    return out


def read_file(path: str, max_length: int = _MAX_READ_BYTES) -> dict[str, Any]:
    p = _resolve_safe_path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(path)
    data = p.read_bytes()[:max_length]
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        text = data.decode('utf-8', errors='ignore')
    return {'path': p.relative_to(_ALLOWED_ROOT).as_posix(), 'content': text, 'truncated': p.stat().st_size > max_length}


def search_files(pattern: str, path: str = '.') -> list[str]:
    base = _resolve_safe_path(path)
    if not base.exists() or not base.is_dir():
        raise FileNotFoundError(path)
    return [p.relative_to(_ALLOWED_ROOT).as_posix() for p in base.rglob(pattern)]


def get_file_info(path: str) -> dict[str, Any]:
    p = _resolve_safe_path(path)
    st = p.stat()
    return {'path': p.relative_to(_ALLOWED_ROOT).as_posix(), 'name': p.name, 'type': 'dir' if p.is_dir() else 'file', 'size': st.st_size, 'exists': p.exists()}


def _is_postgres() -> bool:
    return _db_dialect() == 'postgresql'


# ── 表/列业务注释 ──
_TABLE_META: dict[str, dict[str, Any]] = {
    'sessions': {
        'desc': '用户会话（一个会话 = 一轮完整对话）',
        'columns': {
            'session_id': '会话唯一 ID',
            'user_id': '宿主平台传入的用户标识（非本系统生成）',
            'stage': '当前对话阶段：analyze/select_mode/view_plan/diy_design/image_gen/shop_recommend/order_confirm/done',
            'title': '会话标题（取自用户首条消息前 20 字）',
            'preview': '最新需求快照（JSON 序列化的 FlowerRequirement，用于恢复上下文）',
            'shop_id': '锁定的店铺 ID（用户选定后绑定，整个会话不变）',
            'created_at': '创建时间',
            'updated_at': '最后更新时间',
        },
    },
    'messages': {
        'desc': '对话消息记录（含用户消息、助手回复、工具调用）',
        'columns': {
            'id': '自增主键',
            'session_id': '所属会话 ID',
            'role': '角色：user / assistant / tool',
            'content': '消息文本内容',
            'ui': 'UI 动作类型（assistant 消息携带）：text/plan_card/shop_card/order_card/pay_jump/image_task/dialog_options',
            'data': '结构化数据（JSON，与 ui 类型对应，如 {plans:[...]}/{shops:[...]}/{task_id:...}）',
            'created_at': '创建时间',
        },
    },
    'memories': {
        'desc': '会话级状态存储（键值对，用于跟踪临时状态、标志位）',
        'columns': {
            'id': '自增主键',
            'user_id': '用户标识',
            'category': '分类：存 session_id 表示会话级状态，存 "flag:{session_id}" 表示会话级标志位',
            'key': '键名（如 latest_diy_plan / selected_plan / image_submitted / plan_pushed）',
            'value': '值（JSON 字符串或纯文本）',
            'confidence': '置信度（默认 1.0，预留扩展）',
            'created_at': '创建时间',
            'updated_at': '更新时间',
        },
    },
    'user_preferences': {
        'desc': '用户长期偏好（跨会话持久化，如预算、风格偏好）',
        'columns': {
            'user_id': '用户标识',
            'key': '偏好键（如 budget / recipient / color / style）',
            'value': '偏好值（JSON 字符串）',
            'updated_at': '更新时间',
        },
    },
    'plans': {
        'desc': '花艺方案库（商家预设方案 + DIY 方案模板）',
        'columns': {
            'id': '方案 ID（预设方案如 P001，DIY 方案如 DIY_xxxx）',
            'name': '方案名称（如 "韩式·母亲节花束"）',
            'price': '方案价格（元），预设方案有值，DIY 方案为 0',
            'desc': '方案描述（一句话，含花材与支数）',
            'effect_image_url': '效果图 URL（AI 生成或商家上传）',
            'merchant_name': '商家名称',
            'tags': '标签（JSON 数组或逗号分隔，如 "["玫瑰","母亲节"]"）',
            'style': '风格标签（如 韩式/北欧/自然）',
            'category_id': '分类 ID（关联 categories 表）',
            'rating': '评分（默认 4.8）',
            'sold': '销量',
            'ai_reason': 'AI 推荐理由',
            'created_at': '创建时间',
        },
    },
    'shops': {
        'desc': '花店信息',
        'columns': {
            'id': '店铺 ID（如 S001）',
            'name': '店铺名称',
            'lat': '纬度（用于距离排序）',
            'lng': '经度（用于距离排序）',
            'address': '详细地址',
            'phone': '联系电话',
            'hours': '营业时间',
            'status': '状态：active（正常）/ inactive（关闭）',
            'rating': '评分（默认 4.8）',
            'created_at': '创建时间',
        },
    },
    'shop_plans': {
        'desc': '店铺-方案关联表（多对多，哪些店铺卖哪些方案）',
        'columns': {
            'shop_id': '店铺 ID（联合主键）',
            'plan_id': '方案 ID（联合主键）',
            'stock': '库存数量',
            'status': '上架状态：on（在售）/ off（下架）',
        },
    },
    'categories': {
        'desc': '方案分类',
        'columns': {
            'id': '分类 ID',
            'name': '分类名称',
            'shop_id': '所属店铺（为空表示全局分类）',
            'sort': '排序权重（数字越小越靠前）',
            'created_at': '创建时间',
        },
    },
    'operations_config': {
        'desc': '运营配置（键值对，如配送范围、FAQ、公告）',
        'columns': {
            'key': '配置键',
            'value': '配置值（JSON 字符串）',
        },
    },
    'diy_plans': {
        'desc': 'DIY 方案资产库（用户确认/成交的方案沉淀，可供复用为模板）',
        'columns': {
            'id': '方案 ID（DIY_xxxx）',
            'user_id': '创建者用户 ID',
            'fingerprint': '需求指纹（用于去重）',
            'name': '方案名称',
            'requirement': '原始需求文本',
            'recipient': '送礼对象（母亲/恋人/朋友/自己/长辈/宝宝）',
            'occasion': '场合（生日/母亲节/婚礼/告白/日常等）',
            'style': '风格标签',
            'budget': '预算（元）',
            'color_scheme': '配色方案（JSON 数组，如 ["粉","白","绿"]）',
            'flowers': '花材列表（JSON 数组，含 name/role/qty/unit_price）',
            'packaging': '包装类型（花束/礼盒/瓶插/手捧）',
            'meaning': '花语寓意',
            'diy_steps': '分步插花指引（JSON 数组）',
            'care_tips': '养护建议',
            'card_message': '贺卡寄语文案',
            'card_image_url': '贺卡图片 URL',
            'budget_breakdown': '预算明细（JSON，含 total_estimate/items/fees）',
            'effect_image_url': '效果图 URL',
            'difficulty': '制作难度：入门/进阶/高手',
            'est_time': '预计耗时（分钟）',
            'shelf_life': '保鲜期（如 "约 5-7 天"）',
            'suitable_for': '适宜人群标签（JSON 数组）',
            'caution': '禁忌/提醒',
            'mood_tags': '情绪标签（JSON 数组，如 ["治愈","温柔"]）',
            'status': '状态：confirmed（已确认）/ template（已沉淀为模板）',
            'order_count': '被下单/复用次数（模板排序依据）',
            'source_user_id': '来源用户（成交后记录）',
            'created_at': '创建时间',
            'confirmed_at': '确认时间',
        },
    },
    'notifications': {
        'desc': '通知消息（预留，用于推送订单状态变更等）',
        'columns': {
            'id': '通知 ID',
            'user_id': '接收者用户 ID',
            'type': '通知类型（如 order_status / promotion）',
            'title': '通知标题',
            'body': '通知内容',
            'ref_type': '关联类型（如 order / plan）',
            'ref_id': '关联 ID',
            'push_channel': '推送渠道（默认 inbox 收件箱）',
            'is_read': '是否已读（0 未读 / 1 已读）',
            'created_at': '创建时间',
        },
    },
    'orders': {
        'desc': '订单表（用户下单后生成）',
        'columns': {
            'id': '订单 ID（ORD_xxxx）',
            'user_id': '下单用户 ID',
            'shop_id': '店铺 ID',
            'total': '订单总金额（元）',
            'status': '订单状态：created（已创建）/ paid（已支付）/ shipped（已发货）/ delivered（已送达）/ cancelled（已取消）',
            'card_message': '贺卡寄语',
            'card_image_url': '贺卡图片 URL',
            'pay_jump': '支付跳转参数（JSON，含 order_id/page_path/params）',
            'created_at': '创建时间',
            'updated_at': '更新时间',
        },
    },
    'image_tasks': {
        'desc': '生图任务状态（跨进程/重启持久化）',
        'columns': {
            'task_id': '生图任务 ID',
            'user_id': '任务所属用户 ID',
            'status': '状态：processing / done / failed',
            'prompt': '生图提示词',
            'result_url': '生成结果 URL',
            'error': '失败原因',
            'created_at': '创建时间',
            'updated_at': '最后更新时间',
        },
    },
    'order_items': {
        'desc': '订单明细（每个订单包含的商品项）',
        'columns': {
            'id': '自增主键',
            'order_id': '所属订单 ID',
            'plan_id': '方案 ID',
            'name': '商品名称',
            'price': '单价（元）',
            'quantity': '数量',
        },
    },
}


def list_tables() -> list[str]:
    conn = get_conn()
    rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name").fetchall()
    return [r['table_name'] for r in rows]


def describe_table(table: str) -> dict[str, Any]:
    table = _validate_table(table)
    meta = _TABLE_META.get(table, {})
    conn = get_conn()
    rows = conn.execute(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    if not rows:
        raise ValueError(f'table not found: {table}')
    cols_out = []
    for r in rows:
        col_meta = meta.get('columns', {}).get(r['column_name'], '')
        cols_out.append({'name': r['column_name'], 'type': r['data_type'], 'nullable': r['is_nullable'] == 'YES', 'default': r['column_default'], 'primary_key': False, 'comment': col_meta})
    return {'table': table, 'comment': meta.get('desc', ''), 'columns': cols_out}


def sample_rows(table: str, limit: int = 5) -> list[dict[str, Any]]:
    table = _validate_table(table)
    limit = max(1, min(int(limit or 1), _MAX_SAMPLE_ROWS))
    conn = get_conn()
    rows = conn.execute(f'SELECT * FROM {table} LIMIT {limit}').fetchall()
    return [dict(r) for r in rows]


def query_readonly(sql: str, max_rows: int = _MAX_QUERY_ROWS) -> dict[str, Any]:
    sql = (sql or '').strip().rstrip(';')
    if not sql.lower().startswith('select'):
        raise PermissionError('only SELECT queries are allowed')
    if _DISALLOWED_SQL.search(sql):
        raise PermissionError('disallowed SQL detected')
    conn = get_conn()
    all_rows = conn.execute(sql).fetchall()
    rows = all_rows[:max(1, min(int(max_rows or 1), _MAX_QUERY_ROWS))]
    columns = list(rows[0].keys()) if rows else []
    data = [list(r) for r in rows]
    return {'columns': columns, 'rows': data, 'row_count': len(rows)}


def discover_database(sample_limit: int = 1) -> dict[str, Any]:
    tables = list_tables()
    schema: list[dict[str, Any]] = []
    for t in tables:
        try:
            desc = describe_table(t)
            samples = sample_rows(t, sample_limit) if sample_limit else []
            schema.append({'table': t, 'description': desc, 'sample_rows': samples})
        except Exception as exc:
            schema.append({'table': t, 'error': str(exc)})
    return {'tables': tables, 'schema': schema}


def inspect_source(include_db: bool = True, include_files: bool = True, depth: int = 1, file_exclude: list[str] | None = None) -> dict[str, Any]:
    """一次性概览当前项目文件与数据库结构，适合智能体刚接入时调用。"""
    result: dict[str, Any] = {'root': _ALLOWED_ROOT.as_posix()}
    if include_files:
        try:
            result['files'] = list_files('.', exclude=file_exclude or ['node_modules', '.git', '__pycache__', 'dist', 'build'], depth=depth)
        except Exception as exc:
            result['files_error'] = str(exc)
    if include_db:
        try:
            result['database'] = discover_database(sample_limit=0)
        except Exception as exc:
            result['database_error'] = str(exc)
    return result


def tool_result(ok: bool, data: Any = None, error: str | None = None) -> str:
    return json.dumps({'ok': ok, 'data': data, 'error': error}, ensure_ascii=False)
