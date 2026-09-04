"""数据发现与数据库工具。

只保留一套面向「平台外部数据库」的工具：``platform_db_*``（只读发现/查询/映射管理）。
历史（2026-09 重构）：基于智能体本地 DATABASE_URL 的 ``db_auto_*`` 适配工具已移除——
本地商品/订单镜像全是空表，会误导 LLM 返回空结果；商品数据一律只读平台库。
"""

from __future__ import annotations

from typing import Any

from agent.toolkit import register_tool
from backend.data_gateway.external import discover_external, query_external_entity, sample_external_table, test_external_connection
from backend.data_gateway.mapping_store import get_active_mapping, list_mapping_drafts, save_mapping_draft, update_mapping_status
from backend.data_gateway import generate_mapping_draft, tool_result


@register_tool(name='platform_db_discover', description='只读发现目标平台外部 PostgreSQL 数据库的表、字段、类型、外键和可选脱敏样本，供 AI 生成 data_mapping.json 草案；不会写入目标库。连接凭据只从服务端环境变量读取。', parameters={'type': 'object', 'properties': {'source_id': {'type': 'string', 'description': '外部数据源 ID，对应 PLATFORM_DB_<SOURCE_ID>_URL 环境变量'}, 'schema': {'type': 'string', 'description': '数据库 schema，默认 public'}, 'sample_rows': {'type': 'integer', 'description': '每表样本行数，默认 0，最多 5'}}, 'required': ['source_id']}, tags=['database', 'discovery', 'external'])
def platform_db_discover(source_id: str, schema: str='public', sample_rows: int=0) -> str:
    try:
        return tool_result(True, discover_external(source_id, schema=schema, sample_rows=sample_rows))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='platform_db_test_connection', description='测试目标平台外部数据库连接和服务端身份，仅执行只读探测，不会写入数据。', parameters={'type': 'object', 'properties': {'source_id': {'type': 'string', 'description': '外部数据源 ID，对应 PLATFORM_DB_<SOURCE_ID>_URL 环境变量'}}, 'required': ['source_id']}, tags=['database', 'discovery', 'external'])
def platform_db_test_connection(source_id: str) -> str:
    try:
        return tool_result(True, test_external_connection(source_id))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='platform_db_sample_table', description='从目标平台外部数据库的指定合法表读取最多 5 行脱敏样本，不接受任意 SQL。', parameters={'type': 'object', 'properties': {'source_id': {'type': 'string'}, 'schema': {'type': 'string'}, 'table': {'type': 'string'}, 'limit': {'type': 'integer'}}, 'required': ['source_id', 'schema', 'table']}, tags=['database', 'discovery', 'external'])
def platform_db_sample_table(source_id: str, schema: str='public', table: str='', limit: int=3) -> str:
    try:
        return tool_result(True, sample_external_table(source_id, schema, table, limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='platform_db_query_entity', description='按指定 source_id 的 active 映射只读查询标准业务实体；没有 active 映射、字段白名单或实体映射时拒绝执行。会话已绑定店铺（从某店铺进入）时，结果自动限定在该店铺内，无需也不应再查店铺列表。', parameters={'type': 'object', 'properties': {'source_id': {'type': 'string'}, 'entity': {'type': 'string', 'description': 'plan/shop/order/user'}, 'keyword': {'type': 'string'}, 'limit': {'type': 'integer'}, 'shop_id': {'type': 'string', 'description': '可选：只看该店铺的数据。留空则自动使用会话锁定的店铺（若有）'}}, 'required': ['source_id', 'entity']}, inject_context=True, tags=['database', 'query', 'external'])
def platform_db_query_entity(source_id: str, entity: str, keyword: str='', limit: int=10, shop_id: str='', _context: dict | None=None) -> str:
    # 会话锁定店铺优先兜底：LLM 漏传 shop_id 时也不会漏出跨店数据。
    locked_shop = str((_context or {}).get('shop_id') or '').strip()
    effective_shop = str(shop_id or '').strip() or locked_shop
    # 锁定店铺下不允许越店查别的店（防止模型自作主张查别家商品）。
    if locked_shop and effective_shop != locked_shop:
        return tool_result(False, error=f'当前会话已锁定店铺 {locked_shop}，不允许查询其他店铺（{effective_shop}）的数据')
    try:
        rows = query_external_entity(source_id, entity, keyword, limit, shop_id=effective_shop)
    except Exception as exc:
        return tool_result(False, error=str(exc))
    meta: dict[str, Any] = {}
    if effective_shop:
        meta['shop_scoped'] = True
        meta['shop_id'] = effective_shop
        # 映射缺店铺列时 SQL 层无法过滤，须明确告知模型自行按 shop_id 字段筛选。
        meta['filtered_by'] = 'mapping_shop_column' if rows_scoped_by_sql(entity, rows, effective_shop) else 'none_needs_model_filter'
    return tool_result(True, rows, meta=meta or None)


def rows_scoped_by_sql(entity: str, rows: list[dict[str, Any]], shop_id: str) -> bool:
    """判断结果是否真的按店铺过滤过。

    映射含 shop_id 列时 SQL 已过滤；没有该列时结果未过滤，
    由调用方（模型）根据行内 shop_id 字段自行筛选。空结果视为已过滤（无从漏出）。
    """
    if not rows:
        return True
    return 'shop_id' in (rows[0] or {})


@register_tool(name='platform_mapping_draft', description='根据 platform_db_discover 返回的脱敏 schema profile 生成结构化映射草案，包含候选表字段、证据、置信度和风险；仅 draft，不会写文件、激活映射或写入目标库。', parameters={'type': 'object', 'properties': {'profile': {'type': 'object'}}, 'required': ['profile']}, tags=['database', 'mapping', 'external'])
def platform_mapping_draft(profile: dict[str, Any]) -> str:
    try:
        return tool_result(True, generate_mapping_draft(profile))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='platform_mapping_save_draft', description='将已生成的外部平台映射草案保存到智能体内部 PostgreSQL，自动生成版本并写入审计；不会激活映射或写入目标平台。', parameters={'type': 'object', 'properties': {'profile': {'type': 'object'}, 'draft': {'type': 'object'}, 'actor': {'type': 'string'}}, 'required': ['profile', 'draft']}, tags=['database', 'mapping', 'audit'])
def platform_mapping_save_draft(profile: dict[str, Any], draft: dict[str, Any], actor: str='agent') -> str:
    try:
        return tool_result(True, save_mapping_draft(profile, draft, actor))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='platform_mapping_list_drafts', description='读取指定外部平台数据源的映射草案版本列表，仅返回元数据，不返回敏感连接信息。', parameters={'type': 'object', 'properties': {'source_id': {'type': 'string'}, 'limit': {'type': 'integer'}}, 'required': ['source_id']}, tags=['database', 'mapping', 'audit'])
def platform_mapping_list_drafts(source_id: str, limit: int=20) -> str:
    try:
        return tool_result(True, list_mapping_drafts(source_id, limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='platform_mapping_set_status', description='更新映射草案审核状态：reviewed/approved/active/revoked；状态变更写入内部审计，active 时同数据源旧版本自动撤销。', parameters={'type': 'object', 'properties': {'mapping_id': {'type': 'string'}, 'status': {'type': 'string'}, 'actor': {'type': 'string'}}, 'required': ['mapping_id', 'status', 'actor']}, tags=['database', 'mapping', 'audit'])
def platform_mapping_set_status(mapping_id: str, status: str, actor: str) -> str:
    try:
        return tool_result(True, update_mapping_status(mapping_id, status, actor))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='platform_mapping_get_active', description='读取指定外部数据源当前 active 映射；没有明确激活版本时返回空，不会回退到草案。', parameters={'type': 'object', 'properties': {'source_id': {'type': 'string'}}, 'required': ['source_id']}, tags=['database', 'mapping'])
def platform_mapping_get_active(source_id: str) -> str:
    try:
        return tool_result(True, get_active_mapping(source_id))
    except Exception as exc:
        return tool_result(False, error=str(exc))
