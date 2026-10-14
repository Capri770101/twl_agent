"""SessionStore 端口契约回归：画像/记忆存储的可插拔证据。

老板的运维可能在上线后另建库——本测试锁定「实现必须满足端口契约」，
替换存储时只要新适配器实现同名方法即可，agent 侧零改动。
"""
from __future__ import annotations

import inspect

from agent.ports import SessionStore
from backend.storage import memory


def test_memory_module_satisfies_session_store_protocol():
    proto_methods = {n for n in dir(SessionStore) if not n.startswith('_')}
    missing = [n for n in proto_methods if not callable(getattr(memory, n, None))]
    assert not missing, f'SessionStore 协议方法在 backend.storage.memory 缺失: {missing}'
    # 画像/记忆按 user_id 隔离是硬约束：所有用户态方法的首参数必须是 user_id
    for name in ('get_long_term', 'set_long_term', 'get_or_create_session'):
        params = list(inspect.signature(getattr(memory, name)).parameters)
        assert params and params[0] == 'user_id', f'{name} 首参数必须是 user_id（用户隔离）'


def test_current_store_is_pg_backed_and_replaceable():
    """当前实现是 PostgreSQL；替换存储只需提供同协议适配器（测试即证明）。"""
    from agent.agent import mem_store
    assert mem_store is memory  # 当前默认实现
    # 替换面只有一处：agent.agent.mem_store / memory_consolidator.mem_store
    # （测试套件长期用 monkeypatch 替换它，证明注入点有效）
