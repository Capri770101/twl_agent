"""The real evaluation driver must enter its HTTP client before credential exchange."""
import json
import sys

from scripts import run_eval


def test_eval_main_exchanges_customer_credential_inside_live_client(tmp_path, monkeypatch):
    cases = tmp_path / 'cases.jsonl'
    cases.write_text(json.dumps({'id': 'cs-1', 'message': '查订单', 'customer_credential': True,
                                 'expected_ui': 'text', 'allowed_tools': [], 'forbidden_tools': []}) + '\n',
                     encoding='utf-8')
    output = tmp_path / 'result.json'
    sent = []

    class Response:
        def __init__(self, body):
            self.body = body
        def raise_for_status(self):
            pass
        def json(self):
            return self.body

    class Client:
        active = False
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            self.active = True
            return self
        def __exit__(self, *args):
            self.active = False
        def post(self, url, headers=None, json=None):
            assert self.active
            sent.append((url, headers))
            if url.endswith('/credential'):
                return Response({'data': {'access_token': 'cs-test'}})
            if url.endswith('/auth/token'):
                return Response({'access_token': 'jwt-test', 'user_id': 'u-test'})
            assert headers['X-Customer-Credential'] == 'cs-test'
            return Response({'ui': 'text', 'reply': '请登录平台查询订单。', 'tool_calls': []})

    monkeypatch.setattr(run_eval.httpx, 'Client', Client)
    monkeypatch.setattr(sys, 'argv', ['run_eval.py', '--base-url', 'http://localhost',
                                      '--customer-mock', 'http://mock', '--cases', str(cases),
                                      '--output', str(output), '--strict'])
    assert run_eval.main() == 0
    assert [url.rsplit('/', 1)[-1] for url, _ in sent] == ['credential', 'token', 'chat']
    assert json.loads(output.read_text(encoding='utf-8'))['summary']['passed'] == 1
