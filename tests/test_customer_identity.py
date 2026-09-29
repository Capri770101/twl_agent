import base64, hashlib, hmac, json, time
import pytest
from fastapi import HTTPException
from backend.config import settings
from backend.customer_identity import customer_subject, bound_customer_credential
from backend.auth import derive_platform_user_id

def credential(uid=7, expired=False):
    now = int(time.time()) - (400 if expired else 0)
    payload = {'uid':uid,'iat':now,'exp':now+300,'aud':'twl-customer-service','scope':'orders:read','jti':'test'}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    body = 'cs1.' + encoded
    sig = base64.urlsafe_b64encode(hmac.new(b'x'*32, body.encode(), hashlib.sha256).digest()).decode().rstrip('=')
    return body + '.' + sig

@pytest.fixture(autouse=True)
def configure(monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_SECRET', 'x'*32)

def test_subject_cannot_be_obtained_from_public_identity_exchange():
    token = credential()
    subject = customer_subject(token)
    assert bound_customer_credential(token, subject) == token
    assert derive_platform_user_id('customer_h5', subject) != subject

def test_cross_account_rejected():
    with pytest.raises(HTTPException) as exc:
        bound_customer_credential(credential(8), customer_subject(credential(7)))
    assert exc.value.status_code == 403

@pytest.mark.parametrize('token', [credential(expired=True), 'cs1.bad.signature', credential(True)])
def test_invalid_credential(token):
    with pytest.raises(HTTPException) as exc:
        customer_subject(token)
    assert exc.value.status_code == 401
