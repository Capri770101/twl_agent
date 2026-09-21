import asyncio

import agent.agent as A
from agent.agent import ReActAgent, _is_unbound_image_request


def test_detects_explicit_image_requests_only():
    assert _is_unbound_image_request('给这个方案生成效果图')
    assert _is_unbound_image_request('给「AI 定制花束方案」出图看看')
    assert not _is_unbound_image_request('出图看看')
    assert not _is_unbound_image_request('给我推荐一束花')


def test_missing_diy_image_request_skips_llm_and_product_recommendation(monkeypatch):
    class Memory:
        async def get_or_create_session(self, *args, **kwargs): return 'sid'
        async def get_session_context(self, sid): return {}
        async def get_stage(self, sid): return 'analyze'
        async def get_requirement(self, sid): return None
        async def set_requirement(self, *args): pass
        async def get_long_term(self, uid): return {}
        async def load_history(self, *args, **kwargs): return []
        async def get_session_json(self, *args): return None
        async def save_messages(self, *args): self.saved = args
        async def update_stage(self, *args): pass

    mem = Memory()
    monkeypatch.setattr(A, 'mem_store', mem)
    monkeypatch.setattr(A, 'call_llm', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('LLM called')))
    result = asyncio.run(ReActAgent().run('u', '给这个方案生成效果图', None, None))
    assert result.ui.value == 'text'
    assert '还没有可生成效果图' in result.reply
    assert result.tool_calls == []

