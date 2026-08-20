"""Web 台鉴权：HTTP Basic（用户名/密码来自 config.web）。"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_security = HTTPBasic()


async def basic_auth_dependency(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(_security),
) -> str:
    """校验 Basic Auth；返回用户名。``config.web`` 决定凭据。"""
    cfg = request.app.state.tg.config
    ok_user = secrets.compare_digest(credentials.username.encode(), cfg.web.username.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), cfg.web.password.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="凭据错误",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
