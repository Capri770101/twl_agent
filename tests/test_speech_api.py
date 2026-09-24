"""speech 路由测试：TTS 合成/缓存、ASR 格式校验、鉴权与限流（不发真实网络请求）。"""
import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.auth import current_user, current_user_info
from backend.routers import speech as api


class _Resp:
    def __init__(self, payload=None, content=b'', status=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'http {self.status_code}')


class _Client:
    """伪 httpx.AsyncClient：按调用顺序返回预设响应。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(('post', url, json))
        return self._responses.pop(0)

    async def get(self, url):
        self.calls.append(('get', url))
        return self._responses.pop(0)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api.settings, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(api.settings, 'SPEECH_ENABLED', True)
    monkeypatch.setattr(api.settings, 'SPEECH_API_KEY', 'k')
    monkeypatch.setattr(api.settings, 'DB_PATH', str(tmp_path / 'agent.db'))
    monkeypatch.setattr(api.settings, 'IMAGE_PUBLIC_BASE_URL', '')
    monkeypatch.setattr(api, 'check_rate_limit', lambda *a, **k: (True, 0))
    monkeypatch.setattr(api, '_assert_public_image_url', lambda url: None)
    # 埋点走真实 DB 会失败（best-effort 只记 warning）；测试里直接桩掉，另用专门断言验证调用
    recorded = []
    monkeypatch.setattr(api, 'record_speech_call', lambda *a, **k: recorded.append((a, k)))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[current_user] = lambda: 'u'
    app.dependency_overrides[current_user_info] = lambda: None
    with TestClient(app) as c:
        c.recorded_speech = recorded
        yield c


def _install_httpx(monkeypatch, responses):
    fake = _Client(responses)
    monkeypatch.setattr(api.httpx, 'AsyncClient', lambda *a, **k: fake)
    return fake


def test_tts_synthesizes_and_saves_wav(client, monkeypatch):
    tts_json = {'output': {'audio': {'url': 'https://oss.example.com/a.wav'}}}
    _install_httpx(monkeypatch, [_Resp(tts_json), _Resp(content=b'RIFFfake')])
    r = client.post('/speech/tts', json={'user_id': 'u', 'text': '方案做好了，点保存。'})
    assert r.status_code == 200
    body = r.json()
    assert body['audio_url'].endswith('.wav')
    assert body['cached'] is False
    assert body['ai_generated'] is True


def test_tts_cache_hit_skips_upstream(client, monkeypatch):
    tts_json = {'output': {'audio': {'url': 'https://oss.example.com/a.wav'}}}
    _install_httpx(monkeypatch, [_Resp(tts_json), _Resp(content=b'RIFFfake')])
    first = client.post('/speech/tts', json={'user_id': 'u', 'text': '同一句话'})
    assert first.json()['cached'] is False
    # 第二次不再发上游请求：装一个空响应队列，若被调用会抛 IndexError → 测试失败
    fake = _install_httpx(monkeypatch, [])
    second = client.post('/speech/tts', json={'user_id': 'u', 'text': '同一句话'})
    assert second.status_code == 200
    assert second.json()['cached'] is True
    assert second.json()['audio_url'] == first.json()['audio_url']
    assert fake.calls == []


def test_tts_truncates_overlong_text(client, monkeypatch):
    monkeypatch.setattr(api.settings, 'SPEECH_TTS_MAX_CHARS', 10)
    _install_httpx(monkeypatch, [_Resp({'output': {'audio': {'url': 'https://o/a.wav'}}}), _Resp(content=b'x')])
    r = client.post('/speech/tts', json={'user_id': 'u', 'text': '一二三四五六七八九十百千万'})
    assert r.status_code == 200
    assert len(r.json()['text']) == 10


def test_transcribe_success(client, monkeypatch):
    asr_json = {'output': {'choices': [{'message': {'content': [{'text': ' 我想送妈妈一束花 '}]}}]},
                'usage': {'seconds': 3}}
    _install_httpx(monkeypatch, [_Resp(asr_json)])
    r = client.post(
        '/speech/transcribe?user_id=u',
        files={'file': ('a.wav', b'RIFFdata', 'audio/wav')},
    )
    assert r.status_code == 200
    assert r.json()['text'] == '我想送妈妈一束花'
    # 埋点：kind=asr，units=上游返回的真实秒数
    assert client.recorded_speech, '埋点未触发'
    args, kwargs = client.recorded_speech[-1]
    assert args[2] == 'asr'
    assert kwargs.get('units') == 3
    assert kwargs.get('status') == 'success'


def test_tts_records_metrics_with_char_units(client, monkeypatch):
    tts_json = {'output': {'audio': {'url': 'https://oss.example.com/a.wav'}}}
    _install_httpx(monkeypatch, [_Resp(tts_json), _Resp(content=b'RIFFfake')])
    text = '方案做好了，点保存。'
    r = client.post('/speech/tts', json={'user_id': 'u', 'text': text})
    assert r.status_code == 200
    assert client.recorded_speech, '埋点未触发'
    args, kwargs = client.recorded_speech[-1]
    flat = list(args) + list(kwargs.values())
    assert 'tts' in flat
    assert len(text) in flat  # units = 字符数
    assert False in flat or 'cached' in kwargs  # cached=False（首次合成）


def test_tts_error_records_failure(client, monkeypatch):
    _install_httpx(monkeypatch, [_Resp({}, status=500)])
    r = client.post('/speech/tts', json={'user_id': 'u', 'text': '你好'})
    assert r.status_code == 502
    args, kwargs = client.recorded_speech[-1]
    assert kwargs.get('status') == 'error'


def test_transcribe_rejects_bad_mime_and_empty(client, monkeypatch):
    _install_httpx(monkeypatch, [])
    bad = client.post('/speech/transcribe?user_id=u', files={'file': ('a.txt', b'x', 'text/plain')})
    assert bad.status_code == 415
    empty = client.post('/speech/transcribe?user_id=u', files={'file': ('a.wav', b'', 'audio/wav')})
    assert empty.status_code == 400


def test_identity_mismatch_forbidden(client, monkeypatch):
    _install_httpx(monkeypatch, [_Resp({'output': {'audio': {'url': 'https://o/a.wav'}}}), _Resp(content=b'x')])
    r = client.post('/speech/tts', json={'user_id': 'other', 'text': '你好'})
    assert r.status_code == 403


def test_disabled_returns_503(client, monkeypatch):
    monkeypatch.setattr(api.settings, 'SPEECH_ENABLED', False)
    r = client.post('/speech/tts', json={'user_id': 'u', 'text': '你好'})
    assert r.status_code == 503


def test_rate_limited_returns_429(client, monkeypatch):
    monkeypatch.setattr(api, 'check_rate_limit', lambda *a, **k: (False, 7))
    r = client.post('/speech/tts', json={'user_id': 'u', 'text': '你好'})
    assert r.status_code == 429
