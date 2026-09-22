import asyncio
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen

import pytest
from PIL import Image

from agent.skills import skill_greeting as greeting


@pytest.mark.parametrize('template', ['warm', 'blush', 'green', 'letter', 'night'])
def test_greeting_render_writes_png_and_preserves_plan_context(monkeypatch, tmp_path, template):
    monkeypatch.setattr(greeting.settings, 'DB_PATH', str(tmp_path / 'agent.db'))
    monkeypatch.setattr(greeting.settings, 'IMAGE_PUBLIC_BASE_URL', '')

    async def fake_context(_context):
        return {
            'plan_name': '生日白绿色花束',
            'recipient': '女朋友',
            'occasion': '生日',
            'style': '温柔',
            'colors': '白色、绿色',
            'flowers': '白玫瑰×11、尤加利×2',
            'meaning': '长久陪伴',
            'customer_message': '想表达陪伴和感谢',
        }

    monkeypatch.setattr('agent.greeting_context.current_greeting_context', fake_context)
    raw = asyncio.run(greeting.render_greeting_card(
        '愿你新的一岁被温柔和花香围绕，生日快乐。',
        template=template,
        _context={'user_id': 'u', 'session_id': 's'},
    ))
    result = json.loads(raw)
    assert result['image_url'].startswith('/generated/greet_')
    assert result['recipient'] == '女朋友'
    generated = tmp_path / 'generated'
    files = list(generated.glob('greet_*.png'))
    assert len(files) == 1
    assert files[0].read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert result['plan_context']['flowers'] == '白玫瑰×11、尤加利×2'
    assert result['template'] == template
    with Image.open(files[0]) as image:
        image.load()
        assert image.format == 'PNG'
        assert image.width > 0 and image.height > 0

    # 本地静态 HTTP 验证，不启动业务数据库，也不访问公网。
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f'http://127.0.0.1:{server.server_port}{result["image_url"]}', timeout=5) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == 'image/png'
            assert response.read() == files[0].read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
