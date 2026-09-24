"""speech.py —— 语音端点：TTS 合成 + ASR 转写（阿里云百炼原生接口）。

链路验证（2026-09-24，生产 Key 实测）：
- TTS `qwen3-tts-flash`：POST 原生 multimodal-generation，返回 output.audio.url
  （**带签名的 OSS 临时地址，会过期**，必须立即下载落盘，模式同 Qwen-Image）；
- ASR `qwen3-asr-flash`：同端点，音频以 base64 data URI 传入 input.messages，
  返回 output.choices[0].message.content[0].text。
- OpenAI 兼容的 /audio/speech、/audio/transcriptions 对这两个模型均为 404，
  不要走 compatible-mode。

安全边界：
- 两端点均要求 Bearer 登录凭证 + user_id 归属校验（同 greeting 路由）；
- 按用户维度限流；音频体积/文本长度双重上限；
- TTS 结果按 (model, voice, text) 哈希缓存，同文案不重复合成（省钱省时延）；
- 下载上游音频 URL 前做公网地址校验（复用 tasks 的 SSRF 防护）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from backend.auth import current_user, current_user_info, require_user
from backend.config import settings
from backend.execution import run_worker
from backend.observability import record_speech_call
from backend.rate_limit import check_rate_limit
from backend.storage.object_store import save_generated
from backend.storage.tasks import _assert_public_image_url

router = APIRouter(prefix='/speech', tags=['speech'])
logger = logging.getLogger('speech_api')
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='speech')
_slots = asyncio.Semaphore(2)

_DASHSCOPE_URL = 'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'

_ALLOWED_AUDIO_PREFIXES = (
    'audio/wav', 'audio/x-wav', 'audio/mpeg', 'audio/mp3', 'audio/mp4',
    'audio/m4a', 'audio/x-m4a', 'audio/aac', 'audio/ogg', 'audio/webm',
    'audio/amr', 'audio/silk',
)


class TtsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    user_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=500)


def _api_key() -> str:
    key = (settings.SPEECH_API_KEY or settings.LLM_API_KEY or '').strip()
    if not key:
        raise HTTPException(503, '语音服务未配置 API Key')
    return key


def _require_enabled() -> None:
    if not settings.SPEECH_ENABLED:
        raise HTTPException(503, '语音能力未启用')


def _authorize(uid: str, authenticated: str | None, bucket: str) -> None:
    require_user(uid, authenticated)
    allowed, retry = check_rate_limit(f'{bucket}:{uid}')
    if not allowed:
        raise HTTPException(429, '语音请求过于频繁', headers={'Retry-After': str(retry)})


async def _execute(uid: str, factory):
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=0.2)
    except asyncio.TimeoutError:
        raise HTTPException(503, '语音服务繁忙，请稍后重试')
    try:
        return await asyncio.wait_for(run_worker(_workers, factory, uid, 60), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(504, '语音处理超时')
    except HTTPException:
        raise
    except Exception:
        logger.exception('语音处理失败')
        raise HTTPException(502, '语音处理失败，请稍后重试')
    finally:
        _slots.release()


def _cache_path(digest: str) -> Path:
    return Path(settings.DB_PATH).parent / 'generated' / f'speech_{digest}.wav'


async def synthesize(text: str) -> dict:
    """TTS：合成语音并落盘，返回 {audio_url, cached}。同文案命中缓存直接复用。"""
    model = settings.SPEECH_TTS_MODEL
    voice = settings.SPEECH_TTS_VOICE
    digest = hashlib.sha1(f'{model}|{voice}|{text}'.encode()).hexdigest()[:16]

    if _cache_path(digest).exists():
        base = (settings.IMAGE_PUBLIC_BASE_URL or '').strip()
        url = f'/generated/speech_{digest}.wav'
        return {'audio_url': (base.rstrip('/') + url) if base else url, 'cached': True}

    payload = {
        'model': model,
        'input': {'text': text, 'voice': voice},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            _DASHSCOPE_URL,
            headers={'Authorization': f'Bearer {_api_key()}', 'Content-Type': 'application/json'},
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()

    audio_url = ((result.get('output') or {}).get('audio') or {}).get('url') or ''
    if not audio_url:
        raise RuntimeError(f'TTS 响应缺少音频地址: {str(result)[:200]}')

    # 上游返回的是带签名临时 OSS 地址：先做 SSRF 校验，再立即下载落盘（过期前）
    _assert_public_image_url(audio_url)
    async with httpx.AsyncClient(timeout=60.0) as dl:
        audio_resp = await dl.get(audio_url)
    audio_resp.raise_for_status()
    wav = audio_resp.content
    if len(wav) > 10 * 1024 * 1024:
        raise RuntimeError('TTS 音频超出体积上限')

    saved_url = save_generated(f'speech_{digest}.wav', wav)
    return {'audio_url': saved_url, 'cached': False}


def _wav_duration_seconds(audio: bytes) -> int:
    """从 WAV/PCM 头估算音频秒数（用于 ASR 计费口径统计）。

    标准 RIFF/WAVE：字节 24-27 = 采样率，28-31 = 字节率。非 WAV（mp3/m4a 等）
    或解析失败时回退 0——埋点是 best-effort，估不出秒数不影响转写主流程。
    """
    try:
        if len(audio) < 32 or audio[:4] != b'RIFF':
            return 0
        byte_rate = int.from_bytes(audio[28:32], 'little')
        if byte_rate <= 0:
            return 0
        # data chunk 大小 = 文件总长 - 44（标准头），粗估即可
        data_len = max(0, len(audio) - 44)
        return max(1, round(data_len / byte_rate))
    except Exception:  # noqa: BLE001
        return 0


async def transcribe(audio: bytes, content_type: str) -> dict:
    """ASR：音频字节 → {text, seconds}（qwen3-asr-flash，base64 data URI 传音频）。"""
    mime = (content_type or '').split(';')[0].strip().lower()
    if mime not in _ALLOWED_AUDIO_PREFIXES:
        raise HTTPException(415, f'不支持的音频格式: {mime or "unknown"}')
    if len(audio) > settings.SPEECH_ASR_MAX_BYTES:
        raise HTTPException(413, '音频文件过大，请录制 60 秒以内')
    if not audio:
        raise HTTPException(400, '音频内容为空')

    b64 = base64.b64encode(audio).decode()
    payload = {
        'model': settings.SPEECH_ASR_MODEL,
        'input': {
            'messages': [
                {'role': 'user', 'content': [{'audio': f'data:{mime};base64,{b64}'}]},
            ]
        },
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            _DASHSCOPE_URL,
            headers={'Authorization': f'Bearer {_api_key()}', 'Content-Type': 'application/json'},
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()

    text = ''
    try:
        choices = (result.get('output') or {}).get('choices') or []
        content = (choices[0].get('message') or {}).get('content') or []
        for item in content:
            if isinstance(item, dict) and item.get('text'):
                text = str(item['text']).strip()
                break
    except (IndexError, AttributeError, TypeError):
        text = ''
    if not text:
        raise RuntimeError(f'ASR 响应缺少文字: {str(result)[:200]}')

    # 优先用上游返回的真实秒数（usage.seconds），拿不到再从 WAV 头估算
    seconds = ((result.get('usage') or {}).get('seconds')) or _wav_duration_seconds(audio)
    return {'text': text, 'seconds': int(seconds or 0)}


@router.post('/tts')
async def tts(
    req: TtsRequest,
    authenticated: str | None = Depends(current_user),
    user_info: Any = Depends(current_user_info),
):
    """文本 → 语音。播报文案由调用方生成（见 agent/speech_script.py），本端点只负责合成。"""
    _require_enabled()
    _authorize(req.user_id, authenticated, 'speech-tts')
    text = req.text.strip()
    if not text:
        raise HTTPException(422, '合成文本不能为空')
    if len(text) > settings.SPEECH_TTS_MAX_CHARS:
        # 播报只念操作引导，超长说明调用方把全文塞进来了——截断而不是拒绝，
        # 保证语音模式下用户至少能听到前半段引导。
        text = text[:settings.SPEECH_TTS_MAX_CHARS]

    platform = getattr(user_info, 'platform', None) if user_info else None
    t0 = time.perf_counter()

    async def generate():
        return await synthesize(text)

    try:
        data = await _execute(req.user_id, generate)
    except Exception as exc:
        record_speech_call(req.user_id, platform, 'tts', settings.SPEECH_TTS_MODEL,
                           units=len(text), cached=False, latency_ms=int((time.perf_counter() - t0) * 1000),
                           status='error', error=str(exc))
        raise
    record_speech_call(req.user_id, platform, 'tts', settings.SPEECH_TTS_MODEL,
                       units=len(text), cached=bool(data.get('cached')),
                       latency_ms=int((time.perf_counter() - t0) * 1000), status='success')
    return {'text': text, 'voice': settings.SPEECH_TTS_VOICE, **data, 'ai_generated': True}


@router.post('/transcribe')
async def transcribe_endpoint(
    user_id: str,
    file: UploadFile = File(...),
    authenticated: str | None = Depends(current_user),
    user_info: Any = Depends(current_user_info),
):
    """语音 → 文本。前端录音后上传，转写结果由前端回填到 /chat 的 message。"""
    _require_enabled()
    _authorize(user_id, authenticated, 'speech-asr')
    # 只读上限 + 1 字节，防止把超大上传完整复制到进程内存。
    audio = await file.read(settings.SPEECH_ASR_MAX_BYTES + 1)

    platform = getattr(user_info, 'platform', None) if user_info else None
    t0 = time.perf_counter()

    async def generate():
        return await transcribe(audio, file.content_type or '')

    try:
        result = await _execute(user_id, generate)
    except Exception as exc:
        record_speech_call(user_id, platform, 'asr', settings.SPEECH_ASR_MODEL,
                           units=_wav_duration_seconds(audio), cached=False,
                           latency_ms=int((time.perf_counter() - t0) * 1000), status='error', error=str(exc))
        raise
    record_speech_call(user_id, platform, 'asr', settings.SPEECH_ASR_MODEL,
                       units=int(result.get('seconds') or 0), cached=False,
                       latency_ms=int((time.perf_counter() - t0) * 1000), status='success')
    return {'text': result.get('text', ''), 'ai_generated': True}
