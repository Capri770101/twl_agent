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


def test_render_real_image(client, monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, 'DB_PATH', str(tmp_path / 'unused.db'))
    monkeypatch.setattr(api.settings, 'IMAGE_PUBLIC_BASE_URL', '')
    response = client.post('/greetings/render', json={'user_id':'u','text':'谢谢你的陪伴，生日快乐。','template':'blush'})
    assert response.status_code == 200
    assert response.json()['ui'] == 'greeting_card'
    assert len(list((tmp_path / 'generated').glob('*.png'))) == 1
