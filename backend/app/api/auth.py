"""注册 / 登录 / 登出 / 当前用户。

会话 cookie 的属性不是偏好，是被架构定死的（见 FRD C-2、C-3）：

- ``SameSite=None; Secure`` —— 前端在 Vercel、API 在集群，是**不同站点**；
  默认的 SameSite=Lax 在跨站请求上根本不发送，表现为「登录成功但一刷新就掉线」。
- **host-only（不写 Domain）** —— 同一个 zone 上还跑着别的服务，设成
  domain-wide 会把会话 cookie 发到那些无关主机上，属于凭据泄漏。
- ``HttpOnly`` —— JS 读不到，XSS 偷不走；而 SSE 恰好只能靠 cookie 携带凭据
  （EventSource 不支持自定义请求头）。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_middleware import get_principal, require_principal
from app.config import settings
from app.db import get_session
from app.models.project import CreditLedger, CreditReason, User
from app.models.schemas import LoginRequest, RegisterRequest, UserResponse
from app.services import auth as auth_service

logger = logging.getLogger(__name__)

router = APIRouter()


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="none" if settings.session_cookie_secure else "lax",
        path="/",
        # domain 有意留空 —— host-only，见模块 docstring。
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="none" if settings.session_cookie_secure else "lax",
    )


@router.post("/auth/register", response_model=UserResponse, status_code=201)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """开放自助注册，**赠送 0 点**。

    新用户能登录、能浏览界面，但任何 LLM 功能都会被 402 拒绝，直到管理员定向
    发放点数。这一条消灭了批量注册的经济动机：注册一万个账号也拿不到一分钱
    额度。剩下的只有「占数据库行/抢用户名」这类低危滥用，由 IP 限流兜底。
    """
    ip = auth_service.client_ip(
        dict(request.scope.get("headers") or []),
        request.client.host if request.client else None,
    )
    if not await auth_service.check_register_rate_limit(ip):
        raise HTTPException(
            status_code=429,
            detail=(
                f"注册过于频繁：每个 IP 每 "
                f"{settings.register_rate_limit_window_sec // 60} 分钟最多注册 "
                f"{settings.register_rate_limit} 次，请稍后再试。"
            ),
        )

    existing = (await session.execute(
        select(User).where(User.username == body.username)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="用户名已被占用")

    user = User(
        username=body.username,
        password_hash=auth_service.hash_password(body.password),
        credits=settings.register_grant_credits,
        is_admin=False,
        is_active=True,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="用户名已被占用")

    if settings.register_grant_credits:
        session.add(CreditLedger(
            user_id=user.id,
            delta=settings.register_grant_credits,
            reason=CreditReason.REGISTER.value,
            ref_type="register",
            ref_id=user.id,
        ))
    await session.commit()

    # 注册即登录：签发会话**不受 AUTH_ENFORCED 控制**，否则 P1/P2 阶段无法验证。
    token = await auth_service.create_session(user)
    _set_session_cookie(response, token)
    logger.info("user registered: %s (ip=%s)", user.username, ip)
    return UserResponse(username=user.username, credits=user.credits, is_admin=user.is_admin)


@router.post("/auth/login", response_model=UserResponse)
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    user = (await session.execute(
        select(User).where(User.username == body.username.strip().lower())
    )).scalar_one_or_none()

    # 用户不存在与密码错误必须返回**完全相同**的响应，否则登录接口就成了
    # 用户名枚举器。密码校验照跑一遍（对着一个固定哈希），避免用响应时间
    # 把「用户不存在」区分出来。
    password_ok = auth_service.verify_password(
        body.password,
        user.password_hash if user else auth_service.hash_password("invalid"),
    )
    if user is None or not password_ok or not user.is_active:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = await auth_service.create_session(user)
    _set_session_cookie(response, token)
    return UserResponse(username=user.username, credits=user.credits, is_admin=user.is_admin)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    """服务端销毁会话 + 下发过期 cookie 清掉浏览器侧。"""
    principal = get_principal(request)
    if principal is not None and principal.session_token:
        await auth_service.destroy_session(principal.session_token)
    # 只能 return None：直接返回一个新 Response 会丢掉注入的 response 上设的
    # Set-Cookie（FastAPI 只在返回非 Response 时才合并它的头）。
    _clear_session_cookie(response)
    return None


@router.get("/auth/me", response_model=UserResponse)
async def me(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """当前用户名 + 余额（供前端展示，并在触发计费前提示余额不足）。"""
    principal = require_principal(request)
    if principal.is_guest:
        # 访客不是「登录用户」，这里必须 401。
        #
        # 这一条正是前端零改动的关键：AuthProvider 拿到 401 → user=null → 显示
        # 「登录」而不是「登出 + 余额」；同时它探测强制校验的那个请求会被访客
        # 身份放行（200），于是判定为「未强制」，不把人踢去登录页。结果就是
        # 访客能浏览演示数据、看得到登录入口，而前端一行都不用改。
        raise HTTPException(status_code=401, detail="访客未登录")
    if not principal.is_billable:
        # 兜底：现在每种身份都必然带账号（没绑账号的机器令牌已按未鉴权处理），
        # 走到这里说明冒出了新的无账号身份——按 0 余额返回而不是 500。
        return UserResponse(username=principal.username, credits=0, is_admin=principal.is_admin)

    user = (await session.execute(
        select(User).where(User.id == principal.user_id)
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="账号已不存在")
    return UserResponse(username=user.username, credits=user.credits, is_admin=user.is_admin)
