"""目标平台外部数据库只读连接器。

支持方言：
- postgresql（psycopg，默认）
- mysql（pymysql，只读直连业务库）

所有连接均为只读：连接后立即执行方言对应的「事务只读」指令，且部署侧应为该
账号仅授予 SELECT 权限（不改动目标库结构/数据）。本模块不写入目标库，连接凭据
仅从服务端环境变量读取。
"""
from __future__ import annotations

import datetime
import hashlib
import inspect
import logging
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


logger = logging.getLogger(__name__)

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


def _row_to_dict(row: Any) -> dict[str, Any]:
    """将 DB-API 行（dict / 命名元组）规整为小写键 dict。

    MySQL 的 information_schema 返回大写键（TABLE_NAME / COLUMN_NAME ...），
    而 PostgreSQL 返回小写键；统一小写后下游按小写键访问，跨方言不再 KeyError。
    """
    if row is None:
        return {}
    return {str(k).lower(): v for k, v in dict(row).items()}


# --------------------------------------------------------------------------- #
# 结果转换（展示层规整，按 active 映射的 transforms 配置执行，不写库）
# --------------------------------------------------------------------------- #
def _parse_transform(spec: str) -> tuple[str, Any]:
    """解析转换规则字符串，返回 (name, arg)。

    支持：
    - 'cents_to_yuan'：整型/字符串数值 ÷100，规整为「元」（如 8800 -> 88.0）
    - 'prepend_url:<BASE>'：为相对路径补 CDN 基址（已是 http(s):// 则原样返回）
    """
    if spec and spec.startswith('prepend_url:'):
        return 'prepend_url', spec[len('prepend_url:'):]
    return spec, None


def _apply_one_transform(name: str, arg: Any, value: Any) -> Any:
    if value is None:
        return None
    if name == 'cents_to_yuan':
        try:
            return round(float(value) / 100.0, 2)
        except (TypeError, ValueError):
            return value
    if name == 'prepend_url':
        text = str(value).strip()
        if not text:
            return ''
        if text.lower().startswith(('http://', 'https://')):
            return text
        prefix = (arg or '').rstrip('/')
        return prefix + (text if text.startswith('/') else '/' + text)
    return value


def _apply_transforms(rows: list[dict[str, Any]], transforms: dict[str, str], only=None) -> list[dict[str, Any]]:
    """transforms: {canonical_col: spec}。对查询返回做展示层规整（原地修改后返回）。

    only: 可选字段白名单（set/list）。为 None 时转换全部；否则只转换白名单内的字段。
    用途：下单回读时只对 image 做 CDN 前缀转换（订单卡片要显示图），
    而 price 必须保留原始「分」金额，不能转成「元」，否则平台订单金额错乱。
    """
    if not transforms:
        return rows
    parsed = {col: _parse_transform(spec) for col, spec in transforms.items() if only is None or col in only}
    for row in rows:
        for col, (name, arg) in parsed.items():
            if col in row:
                row[col] = _apply_one_transform(name, arg, row[col])
    return rows


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


# 营业状态实时推算（展示层派生，只读不写库）
#
# 平台库只存静态营业时段文本（如 '07:00-22:00'）与静态 status 字段，无法反映
# 「此刻是否真在营业」。只读架构下不能回写平台库把 status 同步成实时值，因此改为
# 在查询返回前按当前北京时间实时推导，让上层（智能体 / 前端）直接读结论，
# 不必各自比对时间、也不会各自算出不同结果。

_CST = datetime.timezone(datetime.timedelta(hours=8))

# 匹配 07:00-22:00 / 9:00~18:00 / 18:00-次日02:00 等写法；第 3 组为可选的跨天标记
_HOURS_RANGE = re.compile(r'(\d{1,2})\s*:\s*(\d{2})\s*[-~—－至到]\s*(次日|第二天|隔天)?\s*(\d{1,2})\s*:\s*(\d{2})')


def _parse_clock_minutes(hh: str, mm: str) -> int | None:
    """'07','30' -> 450（当日零起分钟数）；非法返回 None。24:00 视为 1440（当日结束）。"""
    try:
        hour, minute = int(hh), int(mm)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 24 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def compute_is_open_now(business_hours: Any, now: datetime.datetime | None = None) -> bool | None:
    """按当前北京时间判断营业时段是否包含此刻。

    返回 True=营业中 / False=已打烊 / None=无法判断（时段文本解析不出任何区间）。
    多段（'09:00-12:00,14:00-18:00'）任一命中即营业；跨天（'18:00-次日02:00'）按跨零点处理。
    解析不出区间时返回 None 而非 False——宁可「未知」，也不能把「看不懂」说成「已打烊」。

    now 仅用于测试注入固定时刻；不传则取当前北京时间。
    """
    if business_hours is None:
        return None
    text = str(business_hours).strip()
    if not text or text.lower() in {'none', 'null', 'nan', '-'}:
        return None
    if now is None:
        now = datetime.datetime.now(_CST)
    now_min = now.hour * 60 + now.minute
    parsed_any = False
    for m in _HOURS_RANGE.finditer(text):
        start = _parse_clock_minutes(m.group(1), m.group(2))
        end = _parse_clock_minutes(m.group(4), m.group(5))
        if start is None or end is None:
            continue
        parsed_any = True
        if end <= start or m.group(3):
            # 跨零点：如 18:00-次日02:00
            if now_min >= start or now_min <= end:
                return True
        elif start <= now_min < end:
            return True
    return False if parsed_any else None


def _annotate_open_status(rows: list[dict[str, Any]]) -> None:
    """对含 business_hours 的行补实时营业状态字段（原地修改，不写库）。

    新增字段：
    - is_open_now：True / False / None（None 表示时段文本无法解析）
    - open_status_text：'营业中' / '已打烊' / 未知说明，供智能体直接引用

    平台库原有的 status 字段保持原样不覆盖——它是平台侧的静态值，可能与推算结果
    不一致；上层应以 is_open_now 为准，并提示「以店铺实际为准」。
    """
    for row in rows:
        if 'business_hours' not in row:
            continue
        flag = compute_is_open_now(row.get('business_hours'))
        row['is_open_now'] = flag
        if flag is True:
            row['open_status_text'] = '营业中'
        elif flag is False:
            row['open_status_text'] = '已打烊'
        else:
            row['open_status_text'] = '未知（营业时段原文无法解析，请按 business_hours 原文如实告知用户）'


def query_external_entity(source_id: str, entity: str, keyword: str = '', limit: int = 10, shop_id: str = '', transform_fields=None) -> list[dict[str, Any]]:
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
    rows = [dict(row) for row in rows]
    transforms = selected.get('transforms') or {}
    only = set(transform_fields) if transform_fields else None
    rows = _apply_transforms(rows, transforms, only=only)
    # 营业状态按「此刻」实时推算（只读派生，不写平台库）
    _annotate_open_status(rows)
    return rows


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
        product = _row_to_dict(conn.execute('SELECT version() AS version').fetchone())
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name LIMIT %s",
            (schema, _MAX_TABLES),
        ).fetchall()
        profile: list[dict[str, Any]] = []
        for table_row in tables:
            table = _row_to_dict(table_row)['table_name']
            if not _IDENTIFIER.match(table):
                continue
            columns = conn.execute(
                'SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns '
                'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position LIMIT %s',
                (schema, table, _MAX_COLUMNS),
            ).fetchall()
            # 外键发现：MySQL 没有 information_schema.constraint_column_usage，
            # 用 key_column_usage 的 referenced_* 列直接拿 FK 目标；任一方言失败时降级为空。
            try:
                if dialect == 'mysql':
                    foreign_keys = conn.execute(
                        "SELECT kcu.column_name, kcu.referenced_table_name AS foreign_table, "
                        "kcu.referenced_column_name AS foreign_column "
                        "FROM information_schema.key_column_usage kcu "
                        "WHERE kcu.referenced_table_name IS NOT NULL "
                        "AND kcu.table_schema=%s AND kcu.table_name=%s",
                        (schema, table),
                    ).fetchall()
                else:
                    foreign_keys = conn.execute(
                        "SELECT kcu.column_name, ccu.table_name AS foreign_table, ccu.column_name AS foreign_column "
                        "FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema "
                        "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name=tc.constraint_name AND ccu.table_schema=tc.table_schema "
                        "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema=%s AND tc.table_name=%s",
                        (schema, table),
                    ).fetchall()
            except Exception as exc:
                logger.warning('[external] 外键发现失败 source=%s table=%s（跳过该表外键）: %s', source_id, table, exc)
                foreign_keys = []
            item: dict[str, Any] = {'table': table, 'columns': [_row_to_dict(c) for c in columns], 'foreign_keys': [_row_to_dict(f) for f in foreign_keys]}
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
