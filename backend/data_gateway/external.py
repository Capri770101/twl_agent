"""目标平台外部数据库只读连接器。"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row

_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MAX_TABLES = 200
_MAX_COLUMNS = 100
_MAX_SAMPLE_ROWS = 5


def _source_url(source_id: str) -> str:
    if not _IDENTIFIER.match(source_id or ''):
        raise ValueError('invalid external source id')
    env_name = f'PLATFORM_DB_{source_id.upper()}_URL'
    url = os.getenv(env_name, '').strip()
    if not url:
        raise RuntimeError(f'未配置外部数据源连接：{env_name}')
    if not url.lower().startswith(('postgresql://', 'postgres://')):
        raise RuntimeError('当前外部数据库连接器仅支持 PostgreSQL，请通过 connector 扩展其他方言')
    return url


def _connect_external(source_id: str):
    conn = psycopg.connect(_source_url(source_id), row_factory=dict_row, connect_timeout=10)
    conn.execute('SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY')
    return conn


def _qualified(schema: str, table: str) -> str:
    if not _IDENTIFIER.match(schema or '') or not _IDENTIFIER.match(table or ''):
        raise ValueError('invalid schema or table')
    return f'"{schema}"."{table}"'


def _redact(value: Any, column: str) -> Any:
    if value is None:
        return None
    text = str(value)
    if any(k in column.lower() for k in ('password', 'token', 'secret', 'phone', 'mobile', 'email', 'address', 'pay')):
        return f'<redacted:{hashlib.sha256(text.encode()).hexdigest()[:10]}>'
    return text[:500] if len(text) > 500 else value


def _safe_limit(value: int, maximum: int) -> int:
    return max(0, min(int(value or 0), maximum))


def test_external_connection(source_id: str) -> dict[str, Any]:
    with _connect_external(source_id) as conn:
        row = conn.execute('SELECT current_database() AS database, current_user AS user, version() AS version').fetchone() or {}
    return {'source_id': source_id, 'dialect': 'postgresql', 'database': row.get('database', ''), 'user': row.get('user', ''), 'server_version': row.get('version', ''), 'read_only_probe': True}


def sample_external_table(source_id: str, schema: str, table: str, limit: int = 3) -> dict[str, Any]:
    limit = max(1, min(int(limit or 1), _MAX_SAMPLE_ROWS))
    with _connect_external(source_id) as conn:
        exists = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s AND table_type='BASE TABLE'", (schema, table)).fetchone()
        if not exists:
            raise ValueError(f'table not found: {schema}.{table}')
        rows = conn.execute(f'SELECT * FROM {_qualified(schema, table)} LIMIT %s', (limit,)).fetchall()
    return {'source_id': source_id, 'schema': schema, 'table': table, 'rows': [{k: _redact(v, k) for k, v in dict(row).items()} for row in rows], 'limit': limit}


def query_external_entity(source_id: str, entity: str, keyword: str = '', limit: int = 10, shop_id: str = '') -> list[dict[str, Any]]:
    """只读查询标准业务实体。

    shop_id 非空时，若该实体的 active 映射含店铺列（canonical 名 shop_id），
    则按店铺过滤——用于「从某家店铺进入」的场景，把结果硬限定在该店铺内。
    映射没有店铺列时无法在 SQL 层过滤，此时返回未过滤结果（调用方需知晓）。
    """
    if not _IDENTIFIER.match(entity or ''):
        raise ValueError('invalid entity')
    from backend.data_gateway.mapping_store import get_active_mapping
    active = get_active_mapping(source_id)
    if not active:
        raise PermissionError('no active mapping for source')
    selected = (active.get('draft_json') or {}).get('entities', {}).get(entity, {}).get('selected', {})
    schema = active.get('schema_name', 'public')
    table = selected.get('table')
    columns = selected.get('columns') or {}
    if not table or not columns:
        raise ValueError(f'active mapping has no usable entity: {entity}')
    if not _IDENTIFIER.match(schema) or not _IDENTIFIER.match(table):
        raise PermissionError('active mapping contains invalid identifier')
    aliases = []
    for canonical, actual in columns.items():
        if not _IDENTIFIER.match(actual):
            raise PermissionError('active mapping contains invalid column')
        aliases.append(f'"{actual}" AS "{canonical}"')
    limit = max(1, min(int(limit or 1), 100))
    sql = f'SELECT {", ".join(aliases)} FROM {_qualified(schema, table)}'
    params: list[Any] = []
    conditions: list[str] = []
    name_col = columns.get('name')
    if keyword and name_col and _IDENTIFIER.match(name_col):
        conditions.append(f'"{name_col}" ILIKE %s')
        params.append(f'%{keyword}%')
    shop_col = columns.get('shop_id')
    if shop_id and shop_col and _IDENTIFIER.match(shop_col):
        conditions.append(f'CAST("{shop_col}" AS text) = %s')
        params.append(str(shop_id))
    if conditions:
        sql += ' WHERE ' + ' AND '.join(conditions)
    sql += ' LIMIT %s'
    params.append(limit)
    with _connect_external(source_id) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def discover_external(source_id: str, schema: str = 'public', sample_rows: int = 0) -> dict[str, Any]:
    """只读发现外部 PostgreSQL 数据库的表结构、字段、外键与可选脱敏样本。

    返回结构化的 schema profile，供 generate_mapping_draft 生成映射草案；
    不会写入目标库，连接凭据仅从服务端环境变量读取。
    """
    if not _IDENTIFIER.match(schema or ''):
        raise ValueError('invalid schema')
    sample_rows = _safe_limit(sample_rows, _MAX_SAMPLE_ROWS)
    with _connect_external(source_id) as conn:
        product = conn.execute('SELECT version() AS version').fetchone()
        tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name LIMIT %s", (schema, _MAX_TABLES)).fetchall()
        profile: list[dict[str, Any]] = []
        for table_row in tables:
            table = table_row['table_name']
            if not _IDENTIFIER.match(table):
                continue
            columns = conn.execute('SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position LIMIT %s', (schema, table, _MAX_COLUMNS)).fetchall()
            foreign_keys = conn.execute("SELECT kcu.column_name, ccu.table_name AS foreign_table, ccu.column_name AS foreign_column FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name=tc.constraint_name AND ccu.table_schema=tc.table_schema WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema=%s AND tc.table_name=%s", (schema, table)).fetchall()
            item: dict[str, Any] = {'table': table, 'columns': [dict(c) for c in columns], 'foreign_keys': [dict(f) for f in foreign_keys]}
            if sample_rows:
                rows = conn.execute(f'SELECT * FROM {_qualified(schema, table)} LIMIT %s', (sample_rows,)).fetchall()
                item['sample_rows'] = [{k: _redact(v, k) for k, v in dict(row).items()} for row in rows]
            profile.append(item)
    fingerprint = hashlib.sha256(repr(profile).encode('utf-8')).hexdigest()
    return {'source_id': source_id, 'dialect': 'postgresql', 'server_version': (product or {}).get('version', ''), 'schema': schema, 'tables': profile, 'schema_fingerprint': fingerprint, 'sample_rows_enabled': bool(sample_rows), 'read_only': True}
