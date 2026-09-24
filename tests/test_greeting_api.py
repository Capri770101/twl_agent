import json
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from backend.routers import greeting as api
from backend.auth import current_user


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api.settings, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(api, 'check_rate_limit', lambda *a: (True, 0))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[current_user] = lambda: 'u'
    with TestClient(app) as client:
        yield client


def test_draft_forwards_only_summary_and_intent(client, monkeypatch):
    def llm(messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        assert 'user_id' not in data
        assert data['items'][0]['name'] == '白玫瑰'
        assert data['customer_intent'] == '谢谢你的陪伴'
        assert kwargs['user_id'] == 'u'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"text":"谢谢你的陪伴，愿每一天都有花香与你相伴。"}'))])
    monkeypatch.setattr(api, 'call_llm', llm)
    response = client.post('/greetings/draft', json={'user_id':'u', 'items':[{'name':'白玫瑰'}], 'customer_intent':'谢谢你的陪伴'})
    assert response.status_code == 200
    assert response.json()['needs_confirmation']


def test_identity_and_sensitive_extras_rejected(client):
    assert client.post('/greetings/draft', json={'user_id':'other','customer_intent':'感谢'}).status_code == 403
    assert client.post('/greetings/draft', json={'user_id':'u','customer_intent':'感谢','phone':'123'}).status_code == 422
    assert client.post('/greetings/render', json={'user_id':'u','text':' '}).status_code == 422


def test_invalid_model_json_is_sanitized(client, monkeypatch):
    monkeypatch.setattr(api, 'call_llm', lambda *a, **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='secret-invalid'))]))
    response = client.post('/greetings/draft', json={'user_id':'u','customer_intent':'感谢'})
    assert response.status_code == 502
    assert 'secret-invalid' not in response.text


def test_render_returns_async_task(client, monkeypatch):
    """贺卡视觉走异步生图任务：响应给 task_id/poll，文字原样回传供前端叠加展示。

    2026-09-24 契约变更：不再同步出图（Pillow 模板合成看起来千篇一律），
    改为 AI 生成花卉背景 + 服务端排版文字，因此这里是任务而非 image_url。
    """
    captured = {}

    async def fake_task(prompt, overlay, user_id=None):
        captured['prompt'] = prompt
        captured['overlay'] = overlay
        captured['user_id'] = user_id
        return 'task123'

    monkeypatch.setattr(api, 'create_greeting_card_task', fake_task)
    response = client.post('/greetings/render', json={'user_id':'u','text':'谢谢你的陪伴，生日快乐。','recipient':'妈妈','sender':'女儿','template':'blush','occasion':'生日'})
    assert response.status_code == 200
    body = response.json()
    assert body['ui'] == 'greeting_card'
    data = body['data']
    assert data['task_id'] == 'task123'
    assert data['poll'] == '/tasks/task123'
    assert data['text'] == '谢谢你的陪伴，生日快乐。'
    assert data['recipient'] == '妈妈'
    assert data['sender'] == '女儿'
    assert data['ai_visual'] is True
    # 生图提示词必须禁止 AI 画字（中文必乱码），文字由服务端排版
    assert '禁止' in captured['prompt']
    assert '文字' in captured['prompt']
    assert captured['overlay']['text'] == '谢谢你的陪伴，生日快乐。'
    assert captured['user_id'] == 'u'
