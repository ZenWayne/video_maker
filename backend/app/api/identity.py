"""端点侧的身份依赖。

访问控制本身**不在这里**——默认拒绝与逐对象归属校验都在
:mod:`app.auth_middleware`，这样漏挂依赖的新路由是「进不去」而不是「裸奔」。
这里只负责把已经解析好的身份交给端点，用于两件事：写归属（owner_id）和扣点数。
"""

from typing import Optional

from fastapi import Header, HTTPException, Request

from app.auth_middleware import get_principal
from app.services.auth import Principal


def current_principal(request: Request) -> Optional[Principal]:
    """当前身份，可能为 None（``AUTH_ENFORCED=false`` 下的匿名调用）。"""
    return get_principal(request)


def require_user(
    request: Request,
    x_user_name: Optional[str] = Header(default=None),
) -> str:
    """调用方的显示名。

    会话身份优先（FR-7 的方向：身份来源改为会话）；没有会话时回落到旧的
    ``X-User-Name`` 头。**回落这一段是过渡期的**：P1 只上后端，现网前端仍在发
    这个头，此时删掉它会把线上打挂。等前端（P2）改造完、强制校验打开（P3），
    这条回落就该连同 header 一起删除。

    ⚠️ ``X-User-Name`` 是**自称**的身份，不校验内容，任何人都能自称任何人。
    它只配用来填展示字段，绝不能用来决定能看见什么——那是 owner_id 的职责。
    """
    principal = get_principal(request)
    if principal is not None:
        return principal.username
    if not x_user_name:
        raise HTTPException(status_code=400, detail="X-User-Name header required")
    return x_user_name
