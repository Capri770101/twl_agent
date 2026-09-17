"""火山方舟（豆包）连通性检查 + 小额度真实调用。

用途：接入豆包（Volcengine Ark）时验证「key 是否有效 / 哪些模型已开通 / 能否真实调用」。
方舟的 OpenAI 兼容端点与项目现有 LLM 通路一致（`LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`
三个环境变量即可切换），所以本脚本既可独立排障，也可作为切换前的体检。

用法::

    # 1) 只看状态（列出模型 + 哪些已开通；未开通的请求不计费）
    ARK_API_KEY=<api-key> python scripts/ark_probe.py

    # 2) 真实调用一次（会产生费用，用于验证链路 / 产生计费记录）
    ARK_API_KEY=<api-key> python scripts/ark_probe.py --call doubao-seed-2-0-lite-260428

key 从环境变量读取，**不接受命令行明文传入**，避免落到 shell history。

排障要点（2026-09-17 实测）：
- `401 AuthenticationError: The API key doesn't exist` → key 无效
- `404 InvalidEndpointOrModel.NotFound` → key 有效但模型名不对/未上线
- `404 ModelNotOpen: Your account xxx has not activated the model` → **key 有效，但要去控制台开通该模型**
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
# 非对话类模型（生图/视频/3D），扫「已开通」时跳过
NON_CHAT = ('seedream', 'seedance', 'seed3d', 'seededit', 'kling', 'wan-')


def _key() -> str:
    key = (os.environ.get('ARK_API_KEY') or '').strip()
    if not key:
        print('✗ 未设置 ARK_API_KEY 环境变量', file=sys.stderr)
        raise SystemExit(2)
    return key


def _post(key: str, path: str, body: dict, timeout: int = 60) -> tuple[bool, dict]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
    )
    try:
        return True, json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'ignore')
        try:
            return False, json.loads(raw)
        except json.JSONDecodeError:
            return False, {'error': {'code': f'HTTP{e.code}', 'message': raw[:200]}}
    except Exception as e:  # noqa: BLE001
        return False, {'error': {'code': type(e).__name__, 'message': str(e)}}


def _get(key: str, path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={'Authorization': f'Bearer {key}'})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))


def chat(key: str, model: str, prompt: str = '只回复两个字：收到', max_tokens: int = 16) -> tuple[bool, dict]:
    """调一次 chat。⚠️ 判定必须**同时看 HTTP 状态与 body**：方舟出错时可能给 HTTP 200
    但 body 里是 `{"error": {...}}`，只看状态码会把失败误判成「已开通」。"""
    ok, payload = _post(key, '/chat/completions', {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
    }, timeout=90)
    if ok and isinstance(payload, dict) and payload.get('error'):
        return False, payload
    return ok, payload


def vision_probe(key: str, model: str, max_tokens: int = 96) -> tuple[bool, dict]:
    """用一张内置生成的 128×128 双色图，验证模型的**多模态视觉**能力。

    为什么要单独测（2026-09-17 实测）：豆包不少模型原生支持图片输入，
    ⚠️ 但**图片有最小尺寸要求（14px）** —— 用太小的图会报
    `InvalidParameter: Image dimensions are too small`，极易被误判成「该模型不支持图片」。
    生成的图是「左红右蓝」，结论可客观核对，不必依赖模型的主观描述。

    Args:
        key: Ark API Key。
        model: 模型 ID。
        max_tokens: 回复上限（视觉请求的 prompt_tokens 包含图片，本身就不小）。

    Returns:
        (是否成功, 原始响应体)；成功时 body 形如 OpenAI chat 响应。
    """
    import base64
    import io

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False, {'error': {'code': 'NoPillow', 'message': '需要 Pillow 生成测试图（pip install pillow）'}}

    img = Image.new('RGB', (128, 128), (220, 30, 30))
    ImageDraw.Draw(img).rectangle([64, 0, 127, 127], fill=(30, 60, 220))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')

    return _post(key, '/chat/completions', {
        'model': model,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': '这张图里有几种颜色？分别在哪个位置？'},
                {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
            ],
        }],
        'max_tokens': max_tokens,
    }, timeout=120)


def list_open_models(key: str) -> list[str]:
    """返回**已开通**的对话模型 ID 列表（探测请求不计费）。"""
    models = _get(key, '/models').get('data', [])
    candidates = [m['id'] for m in models
                  if str(m.get('status', '')).lower() not in ('shutdown', 'retiring')
                  and not any(k in m['id'] for k in NON_CHAT)]
    opened: list[str] = []
    for mid in candidates:
        ok, payload = chat(key, mid)
        if ok:
            opened.append(mid)
        else:
            code = (payload.get('error') or {}).get('code', '')
            marker = {'ModelNotOpen': '未开通', 'InvalidEndpointOrModel.NotFound': '不可用'}.get(code, code)
            print(f'    · {mid:<44} {marker}')
    return opened


def main() -> int:
    ap = argparse.ArgumentParser(description='火山方舟（豆包）连通性检查')
    ap.add_argument('--call', metavar='MODEL', help='真实调用该模型一次（产生费用）')
    ap.add_argument('--list', action='store_true', help='扫描并列出已开通的对话模型')
    ap.add_argument('--vision', metavar='MODEL', help='用内置测试图验证该模型的多模态视觉能力（产生费用）')
    args = ap.parse_args()
    key = _key()

    if args.call:
        print(f'真实调用 {args.call} …（会计费）')
        ok, d = chat(key, args.call)
        if ok:
            content = (d.get('choices') or [{}])[0].get('message', {}).get('content', '')
            print('✓ 调用成功')
            print(f'  model  = {d.get("model")}')
            print(f'  reply  = {content!r}')
            print(f'  usage  = {d.get("usage")}')
            print(f'  id     = {d.get("id")}')
            print('\n→ 这次调用已产生计费记录。')
        else:
            err = d.get('error') or {}
            print(f'✗ 调用失败 [{err.get("code")}] {err.get("message", "")[:200]}')
            return 1
        return 0

    if args.vision:
        print(f'视觉能力测试：{args.vision} …（会计费，prompt 含图片）')
        ok, d = vision_probe(key, args.vision)
        if ok and not (isinstance(d, dict) and d.get('error')):
            content = (d.get('choices') or [{}])[0].get('message', {}).get('content', '')
            print('✓ 支持图片输入（多模态可用）')
            print(f'  reply = {str(content)[:200]!r}')
            print(f'  usage = {d.get("usage")}')
        else:
            err = (d.get('error') or {}) if isinstance(d, dict) else {}
            print(f'✗ 失败 [{err.get("code")}] {str(err.get("message", ""))[:200]}')
            print('  提示：若报 Image dimensions are too small，是图片尺寸问题，不是模型不支持。')
            return 1
        return 0

    print('扫描已开通的对话模型（未开通的请求不计费）…')
    opened = list_open_models(key)
    print()
    if opened:
        print(f'✓ 已开通 {len(opened)} 个：')
        for m in opened:
            print('   ', m)
        print(f'\n调用示例：ARK_API_KEY=... python {os.path.basename(__file__)} --call {opened[0]}')
    else:
        print('✗ 没有任何已开通的对话模型。')
        print('  请到 火山引擎控制台 → 方舟（Ark）→ 模型广场 → 开通至少一个模型。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
