"""语音能力试用脚本：TTS 合成试听 + ASR 转写回环。

用途：接入方/产品在不写任何前端的情况下，直接验证智能体语音链路是否可用、
音色是否满意、转写是否准确，并产生真实用量（可在监控面板「语音用量」看到）。

用法::

    # 本机容器（默认 http://127.0.0.1:8000），演示栈匿名登录
    python scripts/try_speech.py --text "方案做好了，点保存可以存进你的方案。"

    # 生产（走 H5 反代），需要平台 API Key
    python scripts/try_speech.py --base-url http://<H5-IP>/agent \
        --api-key <PLATFORM_KEY> --text "帮你挑了3款花束，点卡片看详情。"

    # 试听 ASR：把任意 wav/mp3 转成文字
    python scripts/try_speech.py --base-url ... --asr-file my_voice.wav

    # 完整回环：TTS 合成 → 保存 wav → 再送 ASR 转写 → 对比原文
    python scripts/try_speech.py --text "..." --roundtrip

产出：
- TTS 音频保存到 --out（默认 ./speech_preview.wav），可直接播放试听；
- 控制台打印每一步的耗时、用量（字符数/秒数）、缓存命中情况。

key 从 --api-key 或环境变量 PLATFORM_API_KEY 读取。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

WAV_MIME = {
    '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.m4a': 'audio/x-m4a',
    '.aac': 'audio/aac', '.ogg': 'audio/ogg', '.webm': 'audio/webm',
    '.amr': 'audio/amr', '.mp4': 'audio/mp4',
}


def _request(url: str, data: bytes | None = None, headers: dict | None = None,
             timeout: int = 90) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _json_body(status: int, raw: bytes) -> dict:
    try:
        return json.loads(raw.decode('utf-8', 'ignore'))
    except json.JSONDecodeError:
        return {'_raw': raw[:300].decode('utf-8', 'ignore'), '_status': status}


def login(base: str, api_key: str) -> tuple[str, str]:
    """换取 (user_id, access_token)。生产走平台 Key，演示栈走匿名。"""
    if api_key:
        status, raw = _request(
            f'{base}/auth/token',
            data=json.dumps({'external_user_id': f'try_speech_{uuid.uuid4().hex[:8]}'}).encode(),
            headers={'X-API-Key': api_key, 'Content-Type': 'application/json'},
            timeout=30,
        )
    else:
        status, raw = _request(f'{base}/auth/anonymous', data=b'{}',
                               headers={'Content-Type': 'application/json'}, timeout=30)
    body = _json_body(status, raw)
    if status != 200 or 'access_token' not in body:
        print(f'✗ 登录失败 HTTP {status}: {body}', file=sys.stderr)
        print('  提示：生产环境需要 --api-key（平台 API Key）；演示栈才可匿名登录。', file=sys.stderr)
        raise SystemExit(2)
    return body['user_id'], body['access_token']


def tts(base: str, token: str, user_id: str, text: str, out: Path) -> dict | None:
    print(f'\n▶ TTS 合成：「{text}」（{len(text)} 字符）')
    t0 = time.perf_counter()
    status, raw = _request(
        f'{base}/speech/tts',
        data=json.dumps({'user_id': user_id, 'text': text}, ensure_ascii=False).encode('utf-8'),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
    )
    elapsed = (time.perf_counter() - t0) * 1000
    body = _json_body(status, raw)
    if status != 200:
        print(f'✗ 合成失败 HTTP {status}: {body}', file=sys.stderr)
        return None
    audio_url = body.get('audio_url') or ''
    print(f'  状态: 200 | 耗时: {elapsed:.0f}ms | 缓存命中: {body.get("cached")} | voice: {body.get("voice")}')

    # 下载音频（相对地址拼 base）
    fetch_url = audio_url if audio_url.startswith('http') else base + audio_url
    s2, audio_bytes = _request(fetch_url, timeout=60)
    if s2 == 200 and audio_bytes:
        out.write_bytes(audio_bytes)
        print(f'  ✓ 音频已保存: {out.resolve()}（{len(audio_bytes) / 1024:.0f} KB）')
        print('    直接用播放器打开试听即可。')
    else:
        print(f'✗ 音频下载失败 HTTP {s2}', file=sys.stderr)
        return None
    return body


def asr(base: str, token: str, user_id: str, audio_file: Path) -> str | None:
    mime = WAV_MIME.get(audio_file.suffix.lower(), 'audio/wav')
    audio = audio_file.read_bytes()
    print(f'\n▶ ASR 转写：{audio_file.name}（{len(audio) / 1024:.0f} KB, {mime}）')

    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{audio_file.name}"\r\n'
        f'Content-Type: {mime}\r\n\r\n'
    ).encode('utf-8') + audio + f'\r\n--{boundary}--\r\n'.encode()

    t0 = time.perf_counter()
    status, raw = _request(
        f'{base}/speech/transcribe?user_id={urllib.parse.quote(user_id)}',
        data=body,
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': f'multipart/form-data; boundary={boundary}'},
        timeout=120,
    )
    elapsed = (time.perf_counter() - t0) * 1000
    result = _json_body(status, raw)
    if status != 200:
        print(f'✗ 转写失败 HTTP {status}: {result}', file=sys.stderr)
        return None
    print(f'  状态: 200 | 耗时: {elapsed:.0f}ms')
    print(f'  ✓ 识别结果：「{result.get("text")}」')
    return result.get('text')


def main() -> None:
    ap = argparse.ArgumentParser(description='语音能力试用（TTS 试听 / ASR 转写 / 回环验证）')
    ap.add_argument('--base-url', default='http://127.0.0.1:8000',
                    help='智能体地址，如 http://127.0.0.1:8000 或 http://<H5-IP>/agent')
    ap.add_argument('--api-key', default=os.environ.get('PLATFORM_API_KEY', ''),
                    help='平台 API Key（生产必填；演示栈留空走匿名登录）')
    ap.add_argument('--text', default='方案做好了，点保存可以存进你的方案。', help='TTS 合成文本')
    ap.add_argument('--out', type=Path, default=Path('speech_preview.wav'), help='TTS 音频保存路径')
    ap.add_argument('--asr-file', type=Path, default=None, help='要转写的音频文件（wav/mp3/m4a 等）')
    ap.add_argument('--roundtrip', action='store_true',
                    help='TTS 合成后把音频送回 ASR，对比识别文本与原文')
    args = ap.parse_args()

    base = args.base_url.rstrip('/')
    user_id, token = login(base, args.api_key)
    print(f'✓ 登录成功 user_id={user_id}')

    if args.asr_file:
        if not args.asr_file.exists():
            print(f'✗ 音频文件不存在: {args.asr_file}', file=sys.stderr)
            raise SystemExit(2)
        if asr(base, token, user_id, args.asr_file) is None:
            raise SystemExit(1)
        return

    result = tts(base, token, user_id, args.text, args.out)
    if not result:
        raise SystemExit(1)

    if args.roundtrip and args.out.exists():
        text = asr(base, token, user_id, args.out)
        if text is None:
            raise SystemExit(1)
        if text is not None:
            same = text.replace(' ', '') == args.text.replace(' ', '')
            print(f'\n▶ 回环对比：{"✓ 一致" if same else "△ 有差异（正常，ASR 会有标点/同音字偏差）"}')
            print(f'  原文：{args.text}')
            print(f'  识别：{text}')

    print('\n完成。用量已计入监控面板「语音用量」卡片（近 24h）。')


if __name__ == '__main__':
    main()
