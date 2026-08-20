"""Web 管理台（FastAPI + Jinja2 + HTMX）。

启动由 main.py 以 uvicorn 后台任务形式拉起；数据来自 core.app.state（内存态实时）
与 persistence.Database（历史/规则/日志）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from web.auth import basic_auth_dependency
from web.views import router

log = logging.getLogger(__name__)
TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(state, db, accounts) -> FastAPI:
    """构造 FastAPI 应用。state/db/accounts 注入到 app.state 供路由使用。"""
    app = FastAPI(title="tg115bot 管理台", docs_url=None, redoc_url=None)
    app.state.tg = state
    app.state.db = db
    app.state.accounts = accounts

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        """健康检查（无需鉴权，供 Docker healthcheck）。"""
        return JSONResponse({"ok": True, "accounts": len(accounts.names())})

    # 受保护路由
    app.include_router(router, dependencies=[Depends(basic_auth_dependency)])
    return app
