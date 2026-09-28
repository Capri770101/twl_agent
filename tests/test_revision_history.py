import asyncio
import copy
import json
from unittest.mock import AsyncMock
import agent.diy_tools as diy
import agent.tools as tools
from backend.storage import memory


def storage(monkeypatch):
    data = {}
    async def get(u, s, k): return copy.deepcopy(data.get((u, s, k)))
    async def put(u, s, k, v): data[u, s, k] = copy.deepcopy(v)
    monkeypatch.setattr(memory, 'get_session_json', get)
    monkeypatch.setattr(memory, 'set_session_json', put)
    return data


def test_restore_and_branch_keep_all_versions(monkeypatch):
    data = storage(monkeypatch)
    ctx = {'user_id': 'u', 'session_id': 's'}
    async def run():
        for i in range(1, 4):
            await diy._store_diy_plan({'plan_id': str(i), 'design': {'packaging': str(i)}}, ctx)
        restored = json.loads(await diy.revise_diy_plan('{}', '用回第一版', ctx))
        assert restored['plan_id'] == '1'
        await diy._store_diy_plan({'plan_id': 'branch', 'parent_id': '1', 'version': 2}, ctx)
        history = data['u', 's', 'diy_plan_versions']
        assert [p['version'] for p in history] == [1, 2, 3, 4]
        assert history[-1]['parent_id'] == '1'
        assert data['u', 's', 'latest_diy_plan']['version'] == 4
    asyncio.run(run())


def test_visual_cache_reuses_and_failed_task_retries(monkeypatch):
    data = storage(monkeypatch)
    ctx = {'user_id': 'u', 'session_id': 's'}
    data['u', 's', 'latest_diy_plan'] = {'plan_id': '1', 'name': '花束', 'design': {'main_flowers': [{'name': '玫瑰', 'qty': 3}]}}
    create = AsyncMock(side_effect=['task1', 'task2'])
    state = AsyncMock(return_value={'status': 'processing'})
    monkeypatch.setattr(tools.tasks, 'create_image_task', create)
    monkeypatch.setattr(tools.tasks, 'get_image_task', state)
    async def run():
        first = json.loads(await tools.generate_effect_image('latest_diy', ctx))
        data['u', 's', 'latest_diy_plan']['name'] = '仅改名称'
        second = json.loads(await tools.generate_effect_image('latest_diy', ctx))
        assert first['task_id'] == second['task_id']
        assert second['reused']
        state.return_value = {'status': 'failed'}
        third = json.loads(await tools.generate_effect_image('latest_diy', ctx))
        assert third['task_id'] == 'task2'
        assert create.await_count == 2
    asyncio.run(run())
