"""Chat rate limiting must not trust headers supplied by untrusted clients."""
from starlette.requests import Request

from backend.routers.chat import _client_ip


def test_client_ip_ignores_spoofed_forwarded_headers():
    request = Request({'type': 'http', 'client': ('203.0.113.44', 1234),
                       'headers': [(b'x-real-ip', b'1.1.1.1'), (b'x-forwarded-for', b'2.2.2.2')]})
    assert _client_ip(request) == '203.0.113.44'
