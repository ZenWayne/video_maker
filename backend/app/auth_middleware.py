"""默认拒绝的鉴权中间件 + 逐对象归属校验。

**为什么是中间件而不是逐端点 Depends。** 仓库里原来的 ``_require_user`` 就是
逐端点挂的，结果 projects.py 里 21 个端点只有 1 个挂上了——漏挂的端点是静默
裸奔，不会有任何报错。放在中间件上，新增路由的默认状态是「进不去」而不是
「不设防」，忘记加依赖会立刻表现为 401 而不是一个安静的漏洞。

同理，**归属校验也在这里做**。只过滤列表是这类改造最典型的漏洞：列表看着干净，
换个 URL 就把别人的项目全拿到了（IDOR）。所有 project 作用域的路径都长
``/api/projects/{id}/...``，在中间件里按路径统一校验，端点就没有「只取不校验」
的可能。

写成裸 ASGI 而不是 ``@app.middleware("http")``：后者是 BaseHTTPMiddleware，会
包住响应体，历史上多次破坏流式响应——本服务有两个 SSE 端点
（``/api/projects/{id}/stream``、``/api/analyses/{id}/stream``），保持它们不被
缓冲是花过力气的。这里只在放行前做判断，从不碰响应体。
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select

from app.config import settings
from app.services.auth import Principal, resolve_principal

logger = logging.getLogger(__name__)

# 免鉴权白名单——**只有这三条**，且不含任何会触发计费的端点。
# /health 不在 /api 前缀下（K8s 探针），天然不经过这里。
PUBLIC_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/register",
})

_PROJECT_PATH_RE = re.compile(r"^/api/projects/(?P<project_id>[^/]+)")


def _unauthorized(detail: str = "Authentication required"):
    return 401, {"error": {"code": "unauthenticated", "message": detail}}


def _not_found():
    # 越权一律回 404 而不是 403：403 等于确认「这个 id 存在」，把别人的项目
    # id 是否有效泄漏给了探测方。
    return 404, {"error": {"code": "not_found", "message": "Project not found"}}


async def _owns_project(principal: Principal, project_id: str) -> bool:
    """当前身份能否操作该项目。

    ``owner_id IS NULL`` 是鉴权上线前的存量数据（P3 迁移前）。开关关闭时放行
    它们，否则 P2 阶段一登录就看到空列表；开关打开时（P3，迁移已完成）一律
    按严格归属处理，NULL 视为不可访问。
    """
    from app import db as db_module
    from app.models.project import Project

    async with db_module.AsyncSession() as session:
        owner_id = (await session.execute(
            select(Project.owner_id).where(Project.id == project_id)
        )).scalar_one_or_none()

    if owner_id is None:
        # 项目不存在，或存量未迁移数据。前者交给端点回它自己的 404
        # （中间件无从区分，也不该抢端点的语义）。
        return not settings.auth_enforced
    return owner_id == principal.user_id


class AuthMiddleware:
    """解析身份 → 默认拒绝 → 归属校验。"""

    def __init__(self, app):
        self.app = app

    async def _reject(self, send, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": payload})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # 预检请求不带 cookie/Authorization，拦下来只会让浏览器把真正的请求
        # 也一起放弃。CORSMiddleware 包在外层，正常情况下预检根本到不了这里。
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        principal = await resolve_principal(headers)

        state = scope.setdefault("state", {})
        state["principal"] = principal

        if path.rstrip("/") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if principal is None:
            if settings.auth_enforced:
                status, body = _unauthorized()
                await self._reject(send, status, body)
                return
            # 开关关闭：与鉴权上线前完全一致地放行，身份为 None
            # （不按用户过滤、不扣点数）。
            await self.app(scope, receive, send)
            return

        # 有身份 → 逐对象归属校验。服务主体（未绑定账号的机器令牌）没有
        # user_id，不参与归属过滤。
        if principal.is_billable:
            match = _PROJECT_PATH_RE.match(path)
            if match and not await _owns_project(principal, match.group("project_id")):
                status, body = _not_found()
                await self._reject(send, status, body)
                return

        await self.app(scope, receive, send)


def get_principal(request) -> Principal | None:
    """FastAPI 依赖：取当前身份（可能是 None —— 见 AUTH_ENFORCED）。"""
    return (request.scope.get("state") or {}).get("principal")


def require_principal(request) -> Principal:
    """FastAPI 依赖：必须有身份，否则 401。

    给「无论开关如何都必须知道你是谁」的端点用（/api/auth/me、管理员接口）。
    """
    from fastapi import HTTPException

    principal = get_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def require_admin(request) -> Principal:
    from fastapi import HTTPException

    principal = require_principal(request)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return principal
