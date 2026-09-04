"""data_gateway 包。

对外只暴露两层能力：
- external.py —— 真正读取平台外部数据库（强制只读），供 ``platform_db_*`` 工具调用；
- mapping_store.py —— 映射草案 / 激活版本的内部存储与状态机（``platform_mapping_*``）；
- mapper.py —— 由脱敏 schema profile 生成映射草案的纯函数（不连任何库）。

历史（2026-09 重构）：gateway.py（基于智能体本地 DATABASE_URL 的 auto_* 查询/下单
适配）已删除——本地商品/订单镜像会误导 LLM 读到空表，商品数据一律改为只读平台库。
"""
from .mapper import generate_mapping_draft, tool_result
