"""宿主无独立认证服务时使用的最小 JWT 认证能力。"""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from fastapi import Header, HTTPException, status

from backend.config import settings


def _secret() -> str:
    if not settings.JWT_SECRET:
        raise HTTPException(status_code=503, detail='服务未配置 JWT_SECRET')
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
        raise HTTPException(status_code=401, detail=f"微信登录失败：{result.get('errmsg', '无效 code')}")
    # 不把微信 openid 原文暴露给业务接口，数据库仍使用稳定 user_id。
    user_id = 'wx_' + hashlib.sha256(result['openid'].encode()).hexdigest()[:24]
    return {'user_id': user_id, 'openid': result['openid'], 'unionid': result.get('unionid')}
