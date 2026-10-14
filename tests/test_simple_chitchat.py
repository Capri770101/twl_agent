import asyncio

import agent.agent as A
import agent.engine.llm as LLM
import agent.engine.understand as UND
from agent.agent import ReActAgent, _is_simple_chitchat


def test_only_short_pure_greeting_is_fast_path():
    assert _is_simple_chitchat('你好')
    assert _is_simple_chitchat('谢谢呀～')
    assert not _is_simple_chitchat('你好，我想送妈妈一束花')
    assert not _is_simple_chitchat('推荐一束花')
    assert not _is_simple_chitchat('在吗，预算300怎么选')


def test_fast_path_skips_llm_and_preserves_contract(monkeypatch):
    class Memory:
        async def get_or_create_session(self, *args, **kwargs): return 'sid'
        async def get_session_context(self, sid): return {}
        async def get_stage(self, sid): return 'analyze'
        async def get_requirement(self, sid): return None
        async def set_requirement(self, *args): pass
        async def get_long_term(self, uid): return {}
        async def load_history(self, *args, **kwargs): return []
        async def save_messages(self, *args): self.saved = args
        async def update_stage(self, *args): pass
        async def update_conversation_preview(self, *args): pass
        async def get_session_json(self, uid, sid, key): return None  # 修改确认门每轮先读草稿（2026-10-10）

    mem = Memory()
    monkeypatch.setattr(A, 'mem_store', mem)
    # v2 靶点（2026-10-14 v1 退役）：LLM 入口分散在 engine.llm（脊柱非流式，调用时读模块属性）
    # 与 engine.understand（理解层，模块级绑定）；寒暄快速通道必须在触达两者之前返回。
    def _boom(*args, **kwargs):
        raise AssertionError('LLM called')
    monkeypatch.setattr(LLM, 'call_llm', _boom)
    monkeypatch.setattr(UND, 'call_llm', _boom)
    monkeypatch.setattr(A, 'call_llm_stream', _boom)
    events = []
    result = asyncio.run(ReActAgent().run('u', '你好', None, None, on_event=events.append))
    assert result.ui.value == 'text'
    assert result.tool_calls == []
    assert '专属花艺小助手' in result.reply
    assert any(event.get('event') == 'text' for event in events)
