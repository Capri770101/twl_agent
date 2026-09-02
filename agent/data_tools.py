"""数据发现与数据库工具。"""

from __future__ import annotations

import json
from typing import Any

from agent.toolkit import register_tool
from backend.data_gateway import (
    auto_create_order,
    auto_list_orders,
    auto_query,
    auto_search_plans,
    auto_search_shops,
    describe_table,
    discover_database,
    get_file_info,
    infer_mapping,
    inspect_source,
    list_files,
    list_tables,
    query_readonly,
    read_file,
    sample_rows,
    search_files,
    tool_result,
)


@register_tool(name='fs_list', description='列出当前项目允许范围内的文件/目录。', parameters={'type': 'object', 'properties': {'path': {'type': 'string', 'description': '起始路径，默认 .'}, 'exclude': {'type': 'array', 'items': {'type': 'string'}, 'description': '排除的目录名或片段'}, 'depth': {'type': 'integer', 'description': '递归深度'}}, 'required': []}, tags=['filesystem'])
def fs_list(path: str='.', exclude: list[str] | None=None, depth: int=2) -> str:
    try:
        return tool_result(True, list_files(path=path, exclude=exclude, depth=depth))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='fs_read', description='读取当前项目允许范围内的文本文件。', parameters={'type': 'object', 'properties': {'path': {'type': 'string', 'description': '文件路径'}, 'max_length': {'type': 'integer', 'description': '最多读取字符数'}}, 'required': ['path']}, tags=['filesystem'])
def fs_read(path: str, max_length: int=200000) -> str:
    try:
        return tool_result(True, read_file(path=path, max_length=max_length))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='fs_search', description='在当前项目允许范围内按 glob 模式搜索文件。', parameters={'type': 'object', 'properties': {'pattern': {'type': 'string', 'description': 'glob 模式，如 **/*.sql'}, 'path': {'type': 'string', 'description': '搜索根目录'}}, 'required': ['pattern']}, tags=['filesystem'])
def fs_search(pattern: str, path: str='.') -> str:
    try:
        return tool_result(True, search_files(path=path, pattern=pattern))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='fs_info', description='查看当前项目允许范围内文件或目录的元信息。', parameters={'type': 'object', 'properties': {'path': {'type': 'string', 'description': '文件或目录路径'}}, 'required': ['path']}, tags=['filesystem'])
def fs_info(path: str) -> str:
    try:
        return tool_result(True, get_file_info(path=path))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_list_tables', description='列出当前数据库中的所有表。', parameters={'type': 'object', 'properties': {}, 'required': []}, tags=['database'])
def db_list_tables() -> str:
    try:
        return tool_result(True, list_tables())
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_describe_table', description='查看数据库表结构。', parameters={'type': 'object', 'properties': {'table': {'type': 'string', 'description': '表名'}}, 'required': ['table']}, tags=['database'])
def db_describe_table(table: str) -> str:
    try:
        return tool_result(True, describe_table(table))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_sample_rows', description='抽样读取数据库表中的少量数据。', parameters={'type': 'object', 'properties': {'table': {'type': 'string', 'description': '表名'}, 'limit': {'type': 'integer', 'description': '样本行数'}}, 'required': ['table']}, tags=['database'])
def db_sample_rows(table: str, limit: int=5) -> str:
    try:
        return tool_result(True, sample_rows(table, limit=limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_query_readonly', description='只读执行 SELECT SQL 查询，禁止写操作。', parameters={'type': 'object', 'properties': {'sql': {'type': 'string', 'description': 'SELECT 语句'}, 'max_rows': {'type': 'integer', 'description': '最大返回行数'}}, 'required': ['sql']}, tags=['database'])
def db_query_readonly(sql: str, max_rows: int=100) -> str:
    try:
        return tool_result(True, query_readonly(sql, max_rows=max_rows))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_discover', description='自动发现当前数据库的表结构与样本数据。', parameters={'type': 'object', 'properties': {'sample_limit': {'type': 'integer', 'description': '每张表的样本行数（默认 1，传 0 只返回结构）'}}, 'required': []}, tags=['database'])
def db_discover(sample_limit: int=1) -> str:
    try:
        return tool_result(True, discover_database(sample_limit=sample_limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='source_inspect', description='一次性概览当前项目文件与数据库表结构，适合刚接入时自动发现数据源。', parameters={'type': 'object', 'properties': {'include_db': {'type': 'boolean', 'description': '是否包含数据库结构'}, 'include_files': {'type': 'boolean', 'description': '是否包含文件列表'}, 'depth': {'type': 'integer', 'description': '文件目录递归深度'}}, 'required': []}, tags=['discovery'])
def source_inspect(include_db: bool=True, include_files: bool=True, depth: int=1) -> str:
    try:
        return tool_result(True, inspect_source(include_db=include_db, include_files=include_files, depth=depth))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_auto_map', description='自动推断当前数据库到标准业务实体（plan/shop/order/user）的字段映射，适合未知数据库接入时先查看映射结果。', parameters={'type': 'object', 'properties': {'force_refresh': {'type': 'boolean', 'description': '是否强制重新推断'}}, 'required': []}, tags=['database', 'adapter'])
def db_auto_map(force_refresh: bool=False) -> str:
    try:
        return tool_result(True, infer_mapping(force_refresh=force_refresh))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_auto_query', description='按自动映射查询标准业务实体（plan/shop/order/user），返回统一字段的数据，适合未知数据库只读查询。', parameters={'type': 'object', 'properties': {'entity': {'type': 'string', 'description': '实体：plan | shop | order | user'}, 'keyword': {'type': 'string', 'description': '可选，按名称模糊搜索'}, 'limit': {'type': 'integer', 'description': '返回行数'}}, 'required': ['entity']}, tags=['database', 'adapter'])
def db_auto_query(entity: str, keyword: str='', limit: int=10) -> str:
    try:
        return tool_result(True, auto_query(entity=entity, keyword=keyword, limit=limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_auto_search_plans', description='按自动映射搜索方案/商品（plan），返回标准字段，适合未知数据库只读搜索现成花束。', parameters={'type': 'object', 'properties': {'keyword': {'type': 'string', 'description': '搜索关键词'}, 'limit': {'type': 'integer', 'description': '返回行数'}}, 'required': []}, tags=['database', 'adapter'])
def db_auto_search_plans(keyword: str='', limit: int=10) -> str:
    try:
        return tool_result(True, auto_search_plans(keyword=keyword, limit=limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_auto_search_shops', description='按自动映射搜索店铺（shop），返回标准字段，适合未知数据库只读搜索店铺。', parameters={'type': 'object', 'properties': {'keyword': {'type': 'string', 'description': '搜索关键词'}, 'limit': {'type': 'integer', 'description': '返回行数'}}, 'required': []}, tags=['database', 'adapter'])
def db_auto_search_shops(keyword: str='', limit: int=10) -> str:
    try:
        return tool_result(True, auto_search_shops(keyword=keyword, limit=limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_auto_create_order', description='按自动映射创建订单。需要 data_mapping.json 中配置 write_enabled=true，否则拒绝写入。', parameters={'type': 'object', 'properties': {'user_id': {'type': 'string', 'description': '用户 ID'}, 'plan_id': {'type': 'string', 'description': '方案/商品 ID'}, 'total_price': {'type': 'number', 'description': '订单总价'}, 'status': {'type': 'string', 'description': '订单状态，默认 created'}, 'order_id': {'type': 'string', 'description': '可选，指定订单 ID'}, 'items': {'type': 'array', 'items': {'type': 'object'}, 'description': '可选订单明细'}}, 'required': ['user_id', 'plan_id', 'total_price']}, tags=['database', 'adapter', 'order'])
def db_auto_create_order(user_id: str, plan_id: str, total_price: float, status: str='created', order_id: str='', items: list[dict] | None=None) -> str:
    try:
        return tool_result(True, auto_create_order(user_id=user_id, plan_id=plan_id, total_price=total_price, status=status, order_id=order_id, items=items))
    except Exception as exc:
        return tool_result(False, error=str(exc))


@register_tool(name='db_auto_list_orders', description='按自动映射读取订单列表，可选按用户过滤。', parameters={'type': 'object', 'properties': {'user_id': {'type': 'string', 'description': '可选，用户 ID'}, 'limit': {'type': 'integer', 'description': '返回行数'}}, 'required': []}, tags=['database', 'adapter', 'order'])
def db_auto_list_orders(user_id: str='', limit: int=10) -> str:
    try:
        return tool_result(True, auto_list_orders(user_id=user_id, limit=limit))
    except Exception as exc:
        return tool_result(False, error=str(exc))
