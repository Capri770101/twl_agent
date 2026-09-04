"""目标平台外部数据库只读连接器。

支持方言：
- postgresql（psycopg，默认）
- mysql（pymysql，只读直连业务库）

所有连接均为只读：连接后立即执行方言对应的「事务只读」指令，且部署侧应为该
账号仅授予 SELECT 权限（不改动目标库结构/数据）。本模块不写入目标库，连接凭据
仅从服务端环境变量读取。
"""
from __future__ import annotations

import hashlib
import inspect
import os
import re
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

try:
    import pymysql
except ImportError:  # 未安装时仅 PostgreSQL 可用，避免硬性依赖
    pymysql = None

_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MAX_TABLES = 200
_MAX_COLUMNS = 100
_MAX_SAMPLE_ROWS = 5

_PG_PREFIXES = ('postgresql://', 'postgres://')
_MYSQL_PREFIXES = ('mysql://', 'mysql+pymysql://')


# --------------------------------------------------------------------------- #
# 方言无关的辅助函数
# --------------------------------------------------------------------------- #
def _source_url(source_id: str) -> str:
    if not _IDENTIFIER.match(source_id or ''):
        raise ValueError('invalid external source id')
    env_name = f'PLATFORM_DB_{source_id.upper()}_URL'
    url = os.getenv(env_name, '').strip()
    if not url:
        raise RuntimeError(f'未配置外部数据源连接：{env_name}')
    low = url.lower()
    if not (low.startswith(_PG_PREFIXES) or low.startswith(_MYSQL_PREFIXES)):
        raise RuntimeError('外部数据源连接仅支持 PostgreSQL / MySQL（postgresql:// 或 mysql://）')
    return url


def _dialect_of(url: str) -> str:
    low = url.lower()
    if low.startswith(_PG_PREFIXES):
        return 'postgresql'
    if low.startswith(_MYSQL_PREFIXES):
        return 'mysql'
    return 'unknown'


def _quote_ident(dialect: str, name: str) -> str:
    """按方言引用单个标识符。"""
    if not _IDENTIFIER.match(name or ''):
        raise ValueError('invalid identifier')
    return f'"{name}"' if dialect == 'postgresql' else f'`{name}`'


def _qualified(dialect: str, schema: str, table: str) -> str:
    if not _IDENTIFIER.match(schema or '') or not _IDENTIFIER.match(table or ''):
        raise ValueError('invalid schema or table')
    return f'{_quote_ident(dialect, schema)}.{_quote_ident(dialect, table)}'


def _current_db_user_sql(dialect: str) -> str:
    # 别名 database / user 在 MySQL 是保留字，必须按方言加引号，否则 1064 语法错。
    if dialect == 'postgresql':
        return 'current_database() AS "database", current_user AS "user", version() AS "version"'
    return 'DATABASE() AS `database`, CURRENT_USER() AS `user`, VERSION() AS `version`'


def _ilike_expr(dialect: str, col_ident: str) -> str:
    """不定词搜索表达式（已引用好的列名）。MySQL 用 LIKE（默认大小写不敏感）。"""
    return f'{col_ident} ILIKE %s' if dialect == 'postgresql' else f'{col_ident} LIKE %s'


def _shop_eq_expr(dialect: str, shop_col_ident: str) -> str:
    """按店铺过滤的表达式（已引用好的列名）。"""
    if dialect == 'postgresql':
        return f'CAST({shop_col_ident} AS text) = %s'
    # MySQL 中店铺列通常为字符串类型，直接等值比较即可
    return f'{shop_col_ident} = %s'


def _redact(value: Any, column: str) -> Any:
    if value is None:
        return None
    text = str(value)
    if any(k in column.lower() for k in ('password', 'token', 'secret', 'phone', 'mobile', 'email', 'address', 'pay')):
        return f'<redacted:{hashlib.sha256(text.encode()).hexdigest()[:10]}>'
    return text[:500] if len(text) > 500 else value


def _safe_limit(value: int, maximum: int) -> int:
    return max(0, min(int(value or 0), maximum))


# --------------------------------------------------------------------------- #
# 连接层
# --------------------------------------------------------------------------- #
def _parse_mysql_url(url: str) -> dict[str, Any]:
    """把 mysql://user:pass@host:port/db?params 解析为 pymysql.connect 参数。"""
    parsed = urlparse(url)
    scheme = parsed.scheme.split('+')[0].lower()
    if scheme != 'mysql':
        raise RuntimeError('非 MySQL 连接串')
    params: dict[str, str] = {}
    if parsed.query:
        for pair in parsed.query.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                params[k.lower()] = v
    kwargs: dict[str, Any] = {
        'host': parsed.hostname or '127.0.0.1',
        'port': parsed.port or 3306,
        'user': parsed.username or '',
        'password': parsed.password or '',
        'database': parsed.path.lstrip('/') or '',
        'connect_timeout': 10,
        'charset': params.get('charset', 'utf8mb4'),
    }
    # SSL 处理（两种场景）：
    #   1) 带证书文件：?ssl_ca=/path&ssl_cert=/path&ssl_key=/path
    #      → 传 ssl={'ca':..,'cert':..,'key':..}（验证服务端证书）
    #   2) 仅要求加密（服务端 REQUIRE SSL，无客户端证书、不验证 CA）：
    #      ?ssl=true / ?ssl=1 / ?sslmode=require / ?sslmode=required
    #      → 优先用 pymysql>=1.1.0 的 ssl_mode='REQUIRED'（仅加密不验证书）；
    #        若安装的是旧分支（无 ssl_mode 参数，如被镜像换成伪装 2.x 的 fork），
    #        退回 ssl={'cert_reqs': CERT_NONE} 同样满足“服务端 REQUIRE SSL，不验证 CA”。
    #        用签名探测而非版本号判断，避免被伪版本号误导。
    certs = {src: params[src] for src in ('ssl_ca', 'ssl_cert', 'ssl_key') if params.get(src)}
    if certs:
        kwargs['ssl'] = certs
    else:
        flag = (params.get('sslmode') or params.get('ssl') or '').lower()
        if flag in ('true', '1', 'yes', 'required', 'require', 'preferred', 'prefer'):
            if _pymysql_supports_ssl_mode():
                kwargs['ssl_mode'] = 'REQUIRED' if flag in ('true', '1', 'yes', 'required', 'require') else 'PREFERRED'
            else:
                import ssl as _ssl
                kwargs['ssl'] = {'cert_reqs': _ssl.CERT_NONE}
    return kwargs


def _pymysql_supports_ssl_mode() -> bool:
    """探测已安装的 pymysql 是否支持 ssl_mode 参数（>=1.1.0 才有）。

    生产容器曾因镜像源换成旧分支（自报 2.2.8、无 ssl_mode）而失败，故用
    签名探测而非 __version__，避免被伪版本号误导。
    """
    if pymysql is None:
        return False
    try:
        sig = inspect.signature(pymysql.connections.Connection.__init__)
        return 'ssl_mode' in sig.parameters
    except Exception:
        return False


class _ExternalConn:
    """抹平 psycopg 与 pymysql 的执行接口，对外暴露统一的 execute/fetch。"""

    def __init__(self, dialect: str, raw: Any):
        self.dialect = dialect
        self._raw = raw
        self._cur: Any = None

    def execute(self, sql: str, params: Any = None) -> Any:
        params = tuple(params) if params else ()
        if self.dialect == 'postgresql':
            return self._raw.execute(sql, params)
        if self._cur is None:
            self._cur = self._raw.cursor()
        self._cur.execute(sql, params)
        return self._cur

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass

    def __enter__(self) -> '_ExternalConn':
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


def _connect_external(source_id: str) -> _ExternalConn:
    url = _source_url(source_id)
    dialect = _dialect_of(url)
    if dialect == 'postgresql':
        conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
        conn.execute('SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY')
        return _ExternalConn('postgresql', conn)
    if dialect == 'mysql':
        if pymysql is None:
            raise RuntimeError('未安装 pymysql，无法连接 MySQL 数据源（请 pip install pymysql）')
        raw = pymysql.connect(**_parse_mysql_url(url), cursorclass=pymysql.cursors.DictCursor)
        with raw.cursor() as cur:
            cur.execute('SET SESSION TRANSACTION READ ONLY')
        return _ExternalConn('mysql', raw)
    raise RuntimeError('不支持的外部数据源方言')


def _resolve_schema(conn: _ExternalConn, dialect: str, schema: str) -> str:
    """MySQL 没有与 PostgreSQL 等价的 'public' 默认 schema；未指定或误用 'public' 时
    回退到当前连接的数据库名。"""
    if dialect == 'mysql' and (not schema or schema == 'public'):
        row = conn.execute('SELECT DATABASE() AS db').fetchone()
        return (row or {}).get('db') or schema
    return schema or 'public'


# --------------------------------------------------------------------------- #
# 对外接口（签名保持不变，内部按方言生成 SQL）
# --------------------------------------------------------------------------- #
def test_external_connection(source_id: str) -> dict[str, Any]:
    dialect = _dialect_of(_source_url(source_id))
    with _connect_external(source_id) as conn:
        row = conn.execute(f'SELECT {_current_db_user_sql(dialect)}').fetchone() or {}
    return {
        'source_id': source_id,
        'dialect': dialect,
        'database': row.get('database', ''),
        'user': row.get('user', ''),
        'server_version': row.get('version', ''),
        'read_only_probe': True,
    }


def sample_external_table(source_id: str, schema: str, table: str, limit: int = 3) -> dict[str, Any]:
    limit = max(1, min(int(limit or 1), _MAX_SAMPLE_ROWS))
    dialect = _dialect_of(_source_url(source_id))
    with _connect_external(source_id) as conn:
        schema = _resolve_schema(conn, dialect, schema)
        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s AND table_type='BASE TABLE'",
            (schema, table),
        ).fetchone()
        if not exists:
            raise ValueError(f'table not found: {schema}.{table}')
        rows = conn.execute(f'SELECT * FROM {_qualified(dialect, schema, table)} LIMIT %s', (limit,)).fetchall()
    return {
        'source_id': source_id,
        'schema': schema,
        'table': table,
        'rows': [{k: _redact(v, k) for k, v in dict(row).items()} for row in rows],
        'limit': limit,
    }


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
    for actual in columns.values():
        if not _IDENTIFIER.match(actual):
            raise PermissionError('active mapping contains invalid column')
    dialect = _dialect_of(_source_url(source_id))
    with _connect_external(source_id) as conn:
        schema = _resolve_schema(conn, dialect, schema)
        aliases = [f'{_quote_ident(dialect, actual)} AS {_quote_ident(dialect, canonical)}'
                   for canonical, actual in columns.items()]
        limit = max(1, min(int(limit or 1), 100))
        sql = f'SELECT {", ".join(aliases)} FROM {_qualified(dialect, schema, table)}'
        params: list[Any] = []
        conditions: list[str] = []
        name_col = columns.get('name')
        if keyword and name_col:
            conditions.append(_ilike_expr(dialect, _quote_ident(dialect, name_col)))
            params.append(f'%{keyword}%')
        shop_col = columns.get('shop_id')
        if shop_id and shop_col:
            conditions.append(_shop_eq_expr(dialect, _quote_ident(dialect, shop_col)))
            params.append(str(shop_id))
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        sql += ' LIMIT %s'
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def discover_external(source_id: str, schema: str = 'public', sample_rows: int = 0) -> dict[str, Any]:
    """只读发现外部数据库的表结构、字段、外键与可选脱敏样本。

    返回结构化的 schema profile，供 generate_mapping_draft 生成映射草案；
    不会写入目标库，连接凭据仅从服务端环境变量读取。
    """
    if not _IDENTIFIER.match(schema or ''):
        raise ValueError('invalid schema')
    sample_rows = _safe_limit(sample_rows, _MAX_SAMPLE_ROWS)
    dialect = _dialect_of(_source_url(source_id))
    with _connect_external(source_id) as conn:
        schema = _resolve_schema(conn, dialect, schema)
        product = conn.execute('SELECT version() AS version').fetchone()
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name LIMIT %s",
            (schema, _MAX_TABLES),
        ).fetchall()
        profile: list[dict[str, Any]] = []
        for table_row in tables:
            table = table_row['table_name']
            if not _IDENTIFIER.match(table):
                continue
            columns = conn.execute(
                'SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns '
                'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position LIMIT %s',
                (schema, table, _MAX_COLUMNS),
            ).fetchall()
            foreign_keys = conn.execute(
                "SELECT kcu.column_name, ccu.table_name AS foreign_table, ccu.column_name AS foreign_column "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name=tc.constraint_name AND ccu.table_schema=tc.table_schema "
                "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema=%s AND tc.table_name=%s",
                (schema, table),
            ).fetchall()
            item: dict[str, Any] = {'table': table, 'columns': [dict(c) for c in columns], 'foreign_keys': [dict(f) for f in foreign_keys]}
            if sample_rows:
                rows = conn.execute(f'SELECT * FROM {_qualified(dialect, schema, table)} LIMIT %s', (sample_rows,)).fetchall()
                item['sample_rows'] = [{k: _redact(v, k) for k, v in dict(row).items()} for row in rows]
            profile.append(item)
    fingerprint = hashlib.sha256(repr(profile).encode('utf-8')).hexdigest()
    return {
        'source_id': source_id,
        'dialect': dialect,
        'server_version': (product or {}).get('version', ''),
        'schema': schema,
        'tables': profile,
        'schema_fingerprint': fingerprint,
        'sample_rows_enabled': bool(sample_rows),
        'read_only': True,
    }
