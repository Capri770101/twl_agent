"""The externally consumed stream must never publish unverified model text."""
import asyncio

from agent.agent import ReActAgent
from tests.test_run_e2e import _install, _terminal


def test_public_stream_contains_only_validated_final_text(monkeypatch):
    # A hallucinated terminal reply must be rejected by the guard before SSE.
    turns = [
        _terminal('respond_to_user', '{"reply":"已经下单成功，价格 88 元", "ui":"text", "data":{}}'),
        _terminal('respond_to_user', '{"reply":"我还不能确认是否下单", "ui":"text", "data":{}}'),
    ]
    _install(monkeypatch, turns, facts='')
    async def consume():
        return [evt async for evt in ReActAgent().arun_stream(
            'u_test', '今天心情不太好，可以陪我聊两句吗？', 'sid_test')]
    events = asyncio.run(asyncio.wait_for(consume(), timeout=15))
    assert events[-1]['event'] == 'done', events
    assert not [evt for evt in events if evt['event'] in ('text_delta', 'text_rollback')]
    text = [evt['content'] for evt in events if evt['event'] == 'text']
    assert len(text) == 1
    assert events[-1]['reply'] == text[0]
    assert '已经下单成功' not in text[0]
