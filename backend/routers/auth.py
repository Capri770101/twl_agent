"""登录与访问令牌接口。

三类接入方式：
1. 微信小程序：POST /auth/wx-login（code 换 token）
2. 其他平台（H5/App/自有后端）：POST /auth/token（X-API-Key + external_user_id 换 token）
3. 匿名（仅开发联调）：POST /auth/anonymous（生产默认关闭）
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.auth import (
    create_access_token,
    current_user,
    derive_platform_user_id,
    login_wechat,
    require_platform_key,
)
from backend.config import settings

router = APIRouter(prefix='/auth', tags=['auth'])


class WechatLoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=512)


class TokenExchangeRequest(BaseModel):
    """平台用户换 token：接入方后端先认证自己的用户，再携带其用户标识来交换。"""
    external_user_id: str = Field(min_length=1, max_length=128)


@router.post('/token')
async def exchange_token(req: TokenExchangeRequest, platform_id: str = Depends(require_platform_key)) -> dict[str, str]:
    """通用 token 交换接口（多平台接入）。

    请求头：X-API-Key: <平台密钥>；请求体：接入方体系内的用户标识。
    信任模型：接入方负责认证终端用户，本接口只负责签发隔离的智能体身份。
    """
    user_id = derive_platform_user_id(platform_id, req.external_user_id)
    return {
        'access_token': create_access_token(user_id),
        'token_type': 'bearer',
        'user_id': user_id,
        'platform_id': platform_id,
    }


@router.post('/anonymous')
async def anonymous_login() -> dict[str, str]:
    if not settings.ANONYMOUS_LOGIN_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='匿名登录已禁用：请使用 /auth/wx-login 或平台 API Key + /auth/token',
        )
    user_id = 'anon_' + secrets.token_urlsafe(18)
    return {'access_token': create_access_token(user_id), 'token_type': 'bearer', 'user_id': user_id}


@router.post('/wx-login')
async def wechat_login(req: WechatLoginRequest) -> dict[str, str]:
    identity = await login_wechat(req.code)
    return {
        'access_token': create_access_token(identity['user_id']),
        'token_type': 'bearer',
        'user_id': identity['user_id'],
    }


@router.get('/me')
async def me(user_id: str | None = Depends(current_user)) -> dict[str, str | bool]:
    if not user_id:
        return {'authenticated': False}
    return {'authenticated': True, 'user_id': user_id}
