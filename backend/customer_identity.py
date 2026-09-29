"""独立客户服务身份，不与客户端可指定的 external_user_id 共享命名空间。"""
import base64
import hashlib
import hmac
import json
import time
from fastapi import HTTPException
from backend.config import settings


def customer_subject(credential: str) -> str:
    secret = settings.CUSTOMER_SERVICE_SECRET
    if not settings.CUSTOMER_SERVICE_ENABLED or len(secret.encode()) < 32:
        raise HTTPException(503, '客户服务身份尚未启用')
    try:
        if len(credential) > 2048:
            raise ValueError()
        prefix, encoded, signature = credential.split('.')
        expected = base64.urlsafe_b64encode(hmac.new(secret.encode(), f'{prefix}.{encoded}'.encode(), hashlib.sha256).digest()).decode().rstrip('=')
        if prefix != 'cs1' or not hmac.compare_digest(signature, expected):
            raise ValueError()
        p = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        now = time.time()
        if (p.get('aud') != 'twl-customer-service' or p.get('scope') != 'orders:read'
            or type(p.get('uid')) is not int or not 0 < p['uid'] <= 9007199254740991
            or type(p.get('iat')) is not int or type(p.get('exp')) is not int
            or p['iat'] > now or p['exp'] <= now or p['exp'] - p['iat'] != 300
            or not isinstance(p.get('jti'), str) or not p['jti']):
            raise ValueError()
        return 'customer_h5_' + hashlib.sha256(str(p['uid']).encode()).hexdigest()
    except (ValueError, TypeError, KeyError):
        raise HTTPException(401, '客户服务凭据无效或已过期') from None


def bound_customer_credential(credential: str, authenticated_user: str | None) -> str:
    if not credential:
        return ''
    if customer_subject(credential) != authenticated_user:
        raise HTTPException(403, '客户服务凭据与当前对话账号不一致')
    return credential
