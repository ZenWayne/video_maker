"""点数：入队前原子预扣、失败退款、管理员发放。

三条铁律（FR-9.2 / FR-9.3）：

1. **入队前扣**。先入队后扣，用户并发发起 N 个任务时每个都能通过余额检查，
   最终透支。所以顺序恒为 校验余额 → 原子扣减 + 写流水 → 入队。
2. **扣减与写流水同一事务**。否则会出现扣了钱查不到出处，或退款重复执行。
3. **失败按分镜粒度退**。Veo 失败是常规路径不是边缘情况；一个分镜失败只退
   那一个分镜的预扣。本仓库的 ``run_shot_pipeline`` 一次只处理一个分镜，所以
   一次入队 = 一笔预扣 = 一个可独立退款的单位。

所有点数事务都跑在**独立 session** 上，不搭端点自己的 session：端点里常有尚未
提交的状态机改动，混在一起 commit 会把无关变更一并落库。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.project import CreditLedger, CreditReason, User
from app.services.auth import Principal

logger = logging.getLogger(__name__)


def video_cost(shot_duration_sec: int) -> int:
    """视频按**秒**计价。

    ShotDuration 是 4|6|8 秒，最长是最短的 2 倍；按分镜计价会让 8 秒分镜白占
    4 秒的便宜。shot_duration 在入队时已知，预扣可以精确算，无需事后调整。
    """
    return settings.credit_cost_video_per_second * int(shot_duration_sec)


def script_cost() -> int:
    return settings.credit_cost_script


def shotlist_cost() -> int:
    return settings.credit_cost_shotlist


def image_cost() -> int:
    return settings.credit_cost_image


def analysis_cost() -> int:
    return settings.credit_cost_analysis


def ai_edit_cost() -> int:
    return settings.credit_cost_ai_edit


class AuthenticationRequired(HTTPException):
    """401：计费操作必须先有身份。

    **这一条不受 AUTH_ENFORCED 控制。** 开关管的是「未认证能不能浏览」，而计费
    操作是另一回事：没有账号就没有余额可扣，放行等于把一条免费的 LLM 通道挂在
    公网上——那正是本 FRD 一开始要解决的问题（"被扫描到只是时间问题，届时是直接
    烧配额"）。FR-3 也明确写了免鉴权白名单"不含任何会触发计费的端点"。

    所以分期上线只对读放宽，对花钱的操作始终收紧。
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=401,
            detail="此操作会调用付费模型，请先登录。",
        )


class InsufficientCredits(HTTPException):
    """402：余额不足。**不入队、不产生任何外部调用。**"""

    def __init__(self, required: int, balance: int):
        super().__init__(
            status_code=402,
            detail=(
                f"点数不足：本次操作需要 {required} 点，当前余额 {balance} 点。"
                "请联系管理员发放点数。"
            ),
        )
        self.required = required
        self.balance = balance


def _session_factory():
    # 运行时取，别在模块顶层 from app.db import AsyncSession：测试会把
    # app.db.AsyncSession 换成内存库的 sessionmaker，早绑定会绕过它。
    from app import db as db_module

    return db_module.AsyncSession


async def reserve(
    principal: Optional[Principal],
    amount: int,
    *,
    ref_type: str,
    ref_id: str,
) -> Optional[str]:
    """原子预扣 *amount* 点并写一条 reserve 流水，返回该流水 id。

    无身份直接抛 :class:`AuthenticationRequired`（401）——计费操作不接受匿名调用，
    见该异常的说明。余额不足抛 :class:`InsufficientCredits`（402）。**调用方必须
    在入队之前调用它，并且把这两个异常原样透传出去。**

    返回 ``None`` 表示本次确实不计费（单价配成了 0，或身份没有账号可扣）。
    """
    if principal is None:
        raise AuthenticationRequired()
    if not principal.is_billable or amount <= 0:
        return None

    async with _session_factory()() as session:
        # 原子性靠 WHERE credits >= :amount：并发的第二个请求会匹配不到行，
        # rowcount=0 → 402。绝不能先 SELECT 再算再 UPDATE。
        result = await session.execute(
            update(User)
            .where(User.id == principal.user_id, User.credits >= amount)
            .values(credits=User.credits - amount)
        )
        if result.rowcount == 0:
            # 余额不足。这里 commit 一个空事务而不是 rollback：本次没有改动任何
            # 行，但 rollback 会作用到**整条连接**上——共用连接时（测试的
            # StaticPool、或任何把连接复用给多个 session 的配置）会把并发那笔
            # 已经扣成功的事务一起回滚掉，表现为「9 个 402，钱却退回来了」。
            await session.commit()
            balance = (await session.execute(
                select(User.credits).where(User.id == principal.user_id)
            )).scalar_one_or_none()
            raise InsufficientCredits(amount, balance or 0)

        entry = CreditLedger(
            user_id=principal.user_id,
            delta=-amount,
            reason=CreditReason.RESERVE.value,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        session.add(entry)
        # 同一事务提交：扣减与流水要么都在，要么都不在。
        await session.commit()
        logger.info(
            "credit reserve: user=%s amount=%d ref=%s:%s entry=%s",
            principal.username, amount, ref_type, ref_id, entry.id,
        )
        return entry.id


def require_identity(principal: Optional[Principal]) -> None:
    """计费端点的**第一道**闸：没身份直接 401。

    reserve/ensure_balance 里也有同样的判断，但那两处的位置取决于端点自己的写法——
    有的端点会先做业务校验（"没有可校准的分镜"→400），匿名调用就会拿到 400 而不是
    401。金额算得出来之前先把身份卡住，才能保证「匿名 = 一律进不去」而不是「看
    你先撞上哪个校验」。
    """
    if principal is None:
        raise AuthenticationRequired()


async def ensure_balance(principal: Optional[Principal], amount: int) -> None:
    """在**任何状态变更之前**先把「余额不足」挡掉，抛 402。

    为什么需要它：真正的扣减必须紧贴入队（见 reserve），但端点在入队前往往已经
    改过状态机、重置过分镜、甚至归档过 storyboard。只靠 reserve 的话，一次 402
    会把项目留在「已推进但没有任务在跑」的半路状态。

    这是一次**只读**预检，不承担防透支职责——防透支仍然是 reserve 的原子扣减。
    残余竞态：两个并发请求都过了预检、只有一个扣成功，输的那个仍会在状态变更
    之后拿到 402。这需要同一项目上余额刚好卡在边界的并发操作，罕见且可重试。

    与 reserve 一样，无身份直接 401：匿名调用连预检都不该过。
    """
    if principal is None:
        raise AuthenticationRequired()
    if not principal.is_billable or amount <= 0:
        return
    async with _session_factory()() as session:
        user = (await session.execute(
            select(User).where(User.id == principal.user_id)
        )).scalar_one_or_none()
        balance = user.credits if user else 0
    if balance < amount:
        raise InsufficientCredits(amount, balance)


def enqueue_kwargs(reservation_id: Optional[str]) -> dict:
    """入队时随任务带上的退款凭据。

    没有预扣就**不传这个参数**，让未计费路径（匿名调用、AUTH_ENFORCED=false）
    的入队调用与鉴权上线前逐字相同 —— P1 的判据就是「线上行为完全一致」。
    """
    return {"reservation_id": reservation_id} if reservation_id else {}


async def refund(reservation_id: Optional[str], *, session_factory=None) -> bool:
    """按预扣流水退款，幂等。

    返回 True 表示本次真的退了；False 表示没有该预扣、或已经退过。
    幂等由 ``uq_credit_ledger_refund``（refund 行的 ref_id 唯一）在**数据库层**
    保证，而不是靠 check-then-insert —— worker 可能并发跑。
    """
    if not reservation_id:
        return False

    factory = session_factory or _session_factory()
    async with factory() as session:
        reservation = (await session.execute(
            select(CreditLedger).where(
                CreditLedger.id == reservation_id,
                CreditLedger.reason == CreditReason.RESERVE.value,
            )
        )).scalar_one_or_none()
        if reservation is None:
            logger.warning("credit refund: 预扣流水 %s 不存在，跳过", reservation_id)
            return False

        amount = -reservation.delta  # reserve 的 delta 是负数
        session.add(CreditLedger(
            user_id=reservation.user_id,
            delta=amount,
            reason=CreditReason.REFUND.value,
            ref_type="reservation",
            ref_id=reservation.id,
        ))
        await session.execute(
            update(User)
            .where(User.id == reservation.user_id)
            .values(credits=User.credits + amount)
        )
        try:
            await session.commit()
        except IntegrityError:
            # 唯一索引挡下了重复退款 —— 这正是期望行为，不是错误。
            await session.rollback()
            logger.info("credit refund: %s 已退过，跳过", reservation_id)
            return False

        logger.info("credit refund: %s 退回 %d 点", reservation_id, amount)
        return True


async def grant(
    username: str,
    delta: int,
    reason_text: Optional[str] = None,
    *,
    ledger_reason: str = CreditReason.GRANT.value,
) -> User:
    """管理员定向发放/回收点数，写一条 grant 流水。delta 可正可负。

    回收（delta<0）不会把余额扣成负数——扣到 0 为止，因为负余额没有任何语义，
    只会让后续所有计费端点报出一个用户看不懂的数字。
    """
    async with _session_factory()() as session:
        user = (await session.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail=f"用户不存在：{username}")

        applied = delta
        if delta < 0:
            applied = -min(user.credits, -delta)

        await session.execute(
            update(User).where(User.id == user.id).values(credits=User.credits + applied)
        )
        session.add(CreditLedger(
            user_id=user.id,
            delta=applied,
            reason=ledger_reason,
            ref_type="admin",
            ref_id=reason_text or None,
        ))
        await session.commit()

        refreshed = (await session.execute(
            select(User).where(User.id == user.id)
        )).scalar_one()
        logger.info("credit grant: user=%s delta=%d → %d", username, applied, refreshed.credits)
        return refreshed


async def balance_of(user_id: str) -> int:
    async with _session_factory()() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        return user.credits if user else 0
