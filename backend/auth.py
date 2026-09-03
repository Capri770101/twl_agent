"""宿主无独立认证服务时使用的最小 JWT 认证能力。"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from fastapi import Header, HTTPException, status

from backend.config import settings

logger = logging.getLogger('auth')


def _secret() -> str:
    if not settings.JWT_SECRET:
        raise RuntimeError('服务未配置 JWT_SECRET，无法签发/校验访问令牌')
    return settings.JWT_SECRET


def create_access_token(user_id: str) -> str:
    now = datetime.now(UTC)
    payload = {
        'sub': user_id,
        'iat': now,
        'exp': now + timedelta(hours=settings.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, _secret(), algorithm='HS256')


def decode_access_token(token: str) -> str:
    try:
        payload = jwt.decode(token, _secret(), algorithms=['HS256'])
        user_id = payload.get('sub')
        if not isinstance(user_id, str) or not user_id:
            raise ValueError('missing subject')
        return user_id
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='登录凭证无效或已过期') from exc


async def current_user(authorization: str | None = Header(default=None)) -> str | None:
    """生产强制 Bearer JWT；开发环境可通过 AUTH_REQUIRED=false 关闭。"""
    if not settings.AUTH_REQUIRED:
        return None
    if not authorization or not authorization.lower().startswith('bearer '):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='需要 Bearer 登录凭证')
    return decode_access_token(authorization.split(' ', 1)[1].strip())


def require_user(user_id: str, authenticated_user: str | None) -> None:
    if settings.AUTH_REQUIRED and authenticated_user != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='无权访问其他用户的数据')


# ── 平台级 API Key 认证（多平台接入）─────────────────────────────

def _parse_platform_keys(raw: str) -> dict[str, str]:
    """解析 PLATFORM_API_KEYS，格式："platform_id=key"，多个用逗号或换行分隔。"""
    keys: dict[str, str] = {}
    for item in raw.replace('\n', ',').split(','):
        item = item.strip()
        if not item or '=' not in item:
            continue
        platform_id, _, key = item.partition('=')
        platform_id = platform_id.strip()
        key = key.strip()
        if platform_id and key:
            keys[platform_id] = key
    return keys


def verify_platform_api_key(api_key: str) -> str | None:
    """校验平台 API Key，匹配返回 platform_id，否则 None。常量时间比较。"""
    if not settings.PLATFORM_API_KEYS or not api_key:
        return None
    for platform_id, expected in _parse_platform_keys(settings.PLATFORM_API_KEYS).items():
        if secrets.compare_digest(api_key, expected):
            return platform_id
    return None


async def require_platform_key(x_api_key: str | None = Header(default=None, alias='X-API-Key')) -> str:
    """平台凭证依赖：校验 X-API-Key，返回 platform_id。

    信任模型：持有 API Key 的接入方（小程序后端/H5 后端/App 后端）负责
    认证自己的终端用户，再通过 POST /auth/token 为其换取智能体 token。
    """
    if not settings.PLATFORM_API_KEYS:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail='服务未配置 PLATFORM_API_KEYS，无法进行平台级认证')
    platform_id = verify_platform_api_key(x_api_key or '')
    if not platform_id:
        logger.warning('[auth] 平台 API Key 校验失败')
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail='平台凭证无效，请检查 X-API-Key')
    return platform_id


def derive_platform_user_id(platform_id: str, external_user_id: str) -> str:
    """从平台标识 + 平台侧用户标识派生稳定的智能体 user_id。

    不落盘原始用户标识（手机号/openid 等），key 轮换不影响 user_id 稳定性。
    """
    safe_platform = ''.join(c if c.isalnum() or c in {'_', '-'} else '_' for c in platform_id)[:32]
    digest = hashlib.sha256(f'{platform_id}:{external_user_id}'.encode()).hexdigest()[:24]
    return f'{safe_platform}_{digest}'


async def login_wechat(code: str) -> dict[str, Any]:
    if not settings.WECHAT_APPID or not settings.WECHAT_SECRET:
        raise HTTPException(status_code=503, detail='未配置 WECHAT_APPID/WECHAT_SECRET')
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            'https://api.weixin.qq.com/sns/jscode2session',
            params={
                'appid': settings.WECHAT_APPID,
                'secret': settings.WECHAT_SECRET,
                'js_code': code,
                'grant_type': 'authorization_code',
            },
        )
    response.raise_for_status()
    result = response.json()
    if result.get('errcode') or not result.get('openid'):
        # 不把微信 API 的原始 errmsg 透传给客户端，避免泄露接口细节。
        logger.warning('[auth] 微信登录失败 errcode=%s', result.get('errcode'))
        raise HTTPException(status_code=401, detail='微信登录失败，请重试')
    # 不把微信 openid 原文暴露给业务接口，数据库仍使用稳定 user_id。
    user_id = 'wx_' + hashlib.sha256(result['openid'].encode()).hexdigest()[:24]
    return {'user_id': user_id, 'openid': result['openid'], 'unionid': result.get('unionid')}
