"""管理员接口：定向发放点数。

本期只有 ``users.is_admin`` **一个布尔位**，不是 RBAC（完整角色模型是明确的
非目标）。点数没有充值/支付链路，发放权只在管理员手里 —— 这正是「开放注册 +
零初始点数」得以与计费安全兼容的另一半。
"""

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_middleware import require_admin
from app.db import get_session
from app.models.project import CreditLedger, User
from app.models.schemas import CreditLedgerEntry, GrantCreditsRequest, UserResponse
from app.services import credits as credits_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/admin/users/{username}/credits", response_model=UserResponse)
async def grant_credits(
    username: str,
    body: GrantCreditsRequest,
    request: Request,
):
    """发放（delta>0）或回收（delta<0）点数，写一条 grant 流水。"""
    admin = require_admin(request)
    user = await credits_service.grant(username, body.delta, body.reason)
    logger.info(
        "admin %s granted %d credits to %s (%s)",
        admin.username, body.delta, username, body.reason or "-",
    )
    return UserResponse(username=user.username, credits=user.credits, is_admin=user.is_admin)


@router.get("/admin/users/{username}/ledger", response_model=list[CreditLedgerEntry])
async def user_ledger(
    username: str,
    request: Request,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    """流水查询 —— 退款/对账的依据（余额本身以 users.credits 为准）。"""
    require_admin(request)
    user = (await session.execute(
        select(User).where(User.username == username)
    )).scalar_one_or_none()
    if user is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"用户不存在：{username}")

    rows = (await session.execute(
        select(CreditLedger)
        .where(CreditLedger.user_id == user.id)
        .order_by(CreditLedger.created_at.desc())
        .limit(max(1, min(limit, 500)))
    )).scalars().all()
    return rows
