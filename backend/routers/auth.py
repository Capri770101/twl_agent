"""登录与访问令牌接口。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.auth import create_access_token, current_user, login_wechat

router = APIRouter(prefix='/auth', tags=['auth'])


class WechatLoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=512)


@router.post('/anonymous')
async def anonymous_login() -> dict[str, str]:
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
async def me(user_id: str | None = Depends(current_user)) -> dict[str, str]:
    if not user_id:
        return {'authenticated': 'false'}
    return {'authenticated': 'true', 'user_id': user_id}
