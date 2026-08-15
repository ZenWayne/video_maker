"""应用级鉴权 P1 的验收测试（FRD 2026-08-11 §6）。

打的是真实 FastAPI 应用、真实 sqlite、真实 redis 会话/限流。唯一被短路的是
会计费的模型调用——那是靠 conftest 的 arq mock 拦在入队处，正是 AC-7 要断言的
那条边界（「队列里没有新任务」，而不是只看返回码）。
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.project import CreditLedger, Project, User
from app.services import credits
from app.services.auth import Principal, hash_password
from tests.integration.conftest import _add_shots, register_test_project

PASSWORD = "test-password-123"


def unique_ip() -> str:
    """每次调用给一个全新的来源 IP。

    限流计数器在 redis 里活一小时，跨测试轮次会残留；固定 IP 会让上一轮的
    计数把这一轮的第一次注册就挡掉，表现为莫名其妙的 flaky。
    """
    n = uuid.uuid4().int
    return f"198.51.{(n >> 8) % 256}.{n % 256}"


@pytest.fixture
async def auth_client(client, redis, monkeypatch):
    """会话/限流走真实 redis：中间件读的是 app.main 的全局客户端。"""
    import app.main as app_main

    monkeypatch.setattr(app_main, "_redis_client", redis)
    # 默认关闭强制校验 —— 与 P1 上线时的线上配置一致。
    monkeypatch.setattr(settings, "auth_enforced", False)
    monkeypatch.setattr(settings, "machine_token", "")
    monkeypatch.setattr(settings, "machine_token_user", "")
    return client


async def make_user(sf, username, *, credits_=0, is_admin=False, is_active=True) -> str:
    async with sf() as s:
        user = User(
            username=username,
            password_hash=hash_password(PASSWORD),
            credits=credits_,
            is_admin=is_admin,
            is_active=is_active,
        )
        s.add(user)
        await s.commit()
        return user.id


async def login(c, username) -> dict:
    """登录并返回可直接用的 Cookie 头。

    不依赖 httpx 的 cookie jar：会话 cookie 是 Secure 的，而测试走的是
    http://test，jar 会（正确地）拒绝回发它。手动带头既绕开这点，也让
    「cookie 属性」本身能被单独断言。
    """
    r = await c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.cookies.get(settings.session_cookie_name) or _cookie_from_header(r)
    return {"Cookie": f"{settings.session_cookie_name}={token}"}


def _cookie_from_header(response) -> str:
    raw = response.headers["set-cookie"]
    return raw.split(";")[0].split("=", 1)[1]


async def owned_project(sf, owner_id, *, status="draft", shots=0) -> str:
    async with sf() as s:
        p = Project(
            title="Owned", theme_text="theme", creator_name="owner",
            owner_id=owner_id, status=status,
        )
        s.add(p)
        await s.commit()
        pid = p.id
    if shots:
        # 视频类端点按「即将生成的那个分镜」的时长计价，没有分镜就无从扣费。
        await _add_shots(sf, pid, count=shots, status="pending")
    return register_test_project(pid)


# ── AC-1 / AC-6：默认拒绝 ───────────────────────────────────────────────────

async def test_ac1_unauthenticated_is_rejected_when_enforced(auth_client, monkeypatch):
    r = await auth_client.get("/api/projects")
    assert r.status_code == 200, "AUTH_ENFORCED=false 时必须与鉴权上线前完全一致"

    monkeypatch.setattr(settings, "auth_enforced", True)
    r = await auth_client.get("/api/projects")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


async def test_ac1_billing_endpoints_are_rejected_unauthenticated(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "auth_enforced", True)
    for method, path in [
        ("post", "/api/projects/any-id/start"),
        ("post", "/api/projects/any-id/regenerate-script"),
        ("post", "/api/projects/any-id/approve-script"),
        ("post", "/api/projects/any-id/shots/1/generate-tail-frame"),
        ("post", "/api/projects/any-id/shots/1/voice-convert"),
        ("get", "/api/projects/any-id/stream"),
        ("post", "/api/analyses"),
        ("get", "/api/analyses"),
    ]:
        r = await getattr(auth_client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} 未认证却不是 401"


async def test_ac6_route_without_explicit_dependency_is_still_denied(auth_client, monkeypatch):
    """默认拒绝是全局兜底，不是逐端点挂载。

    新增一条**完全不带任何鉴权依赖**的路由，它必须依然 401 —— 这正是
    _require_user 当年只覆盖 1 个端点的那类漏洞的回归测试。
    """
    from app.main import app

    @app.get("/api/__test_naked_route")
    async def _naked():  # noqa: ANN202
        return {"leaked": True}

    try:
        monkeypatch.setattr(settings, "auth_enforced", True)
        r = await auth_client.get("/api/__test_naked_route")
        assert r.status_code == 401, "漏挂依赖的新路由必须进不去，而不是裸奔"

        monkeypatch.setattr(settings, "auth_enforced", False)
        assert (await auth_client.get("/api/__test_naked_route")).status_code == 200
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes
            if getattr(r, "path", None) != "/api/__test_naked_route"
        ]


async def test_health_and_auth_endpoints_stay_public(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "auth_enforced", True)
    assert (await auth_client.get("/health")).status_code == 200
    # 登录/注册本身必须够得着，否则没人能登进来。
    assert (await auth_client.post("/api/auth/login", json={
        "username": "nobody", "password": "x"})).status_code == 401
    assert (await auth_client.post("/api/auth/register", json={
        "username": "ab", "password": "short"})).status_code == 422


# ── 注册 / 登录 / 登出 / me ─────────────────────────────────────────────────

async def test_register_login_logout_flow(auth_client, db_session_factory):
    r = await auth_client.post(
        "/api/auth/register",
        json={"username": "alice", "password": PASSWORD},
        headers={"CF-Connecting-IP": unique_ip()},
    )
    assert r.status_code == 201
    assert r.json() == {"username": "alice", "credits": 0, "is_admin": False}

    # 口令绝不能明文入库
    async with db_session_factory() as s:
        user = (await s.execute(select(User).where(User.username == "alice"))).scalar_one()
    assert PASSWORD not in user.password_hash

    headers = await login(auth_client, "alice")
    me = await auth_client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["username"] == "alice"

    logout = await auth_client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 204
    # 会话在服务端失效，旧 cookie 立刻不可用
    assert (await auth_client.get("/api/auth/me", headers=headers)).status_code == 401


async def test_session_cookie_attributes_are_cross_site_safe(auth_client):
    """C-2 / C-3：属性写错的表现是「登录成功但一刷新就掉线」或凭据泄漏。"""
    await auth_client.post(
        "/api/auth/register",
        json={"username": "cookieuser", "password": PASSWORD},
        headers={"CF-Connecting-IP": unique_ip()},
    )
    r = await auth_client.post(
        "/api/auth/login", json={"username": "cookieuser", "password": PASSWORD}
    )
    raw = r.headers["set-cookie"].lower()
    assert "httponly" in raw, "JS 读得到会话就防不住 XSS 窃取"
    assert "secure" in raw
    assert "samesite=none" in raw, "SameSite=Lax 在跨站请求上根本不发送"
    assert "path=/" in raw
    assert "domain=" not in raw, "必须 host-only：domain-wide 会把会话发到同 zone 的无关主机"


async def test_login_does_not_distinguish_unknown_user_from_wrong_password(
    auth_client, db_session_factory
):
    await make_user(db_session_factory, "known")
    a = await auth_client.post("/api/auth/login", json={"username": "known", "password": "nope"})
    b = await auth_client.post("/api/auth/login", json={"username": "ghost", "password": "nope"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json(), "响应可区分 = 登录接口成了用户名枚举器"


async def test_inactive_user_cannot_log_in(auth_client, db_session_factory):
    await make_user(db_session_factory, "disabled", is_active=False)
    r = await auth_client.post(
        "/api/auth/login", json={"username": "disabled", "password": PASSWORD}
    )
    assert r.status_code == 401


async def test_session_ttl_slides_on_each_request(auth_client, db_session_factory, redis):
    """7 天滑动过期：TTL 从最后一次请求起算，不是从登录起算。"""
    await make_user(db_session_factory, "slider")
    headers = await login(auth_client, "slider")
    token = headers["Cookie"].split("=", 1)[1]
    key = f"session:{token}"

    await redis.expire(key, 60)
    assert await redis.ttl(key) <= 60
    assert (await auth_client.get("/api/auth/me", headers=headers)).status_code == 200
    assert await redis.ttl(key) > 60 * 60 * 24, "会话没有滑动续期"


# ── AC-11：注册限流（FR-10） ────────────────────────────────────────────────

async def test_ac11_second_registration_from_same_ip_is_rate_limited(auth_client):
    ip = {"CF-Connecting-IP": unique_ip()}
    assert (await auth_client.post(
        "/api/auth/register", json={"username": "first", "password": PASSWORD}, headers=ip
    )).status_code == 201
    r = await auth_client.post(
        "/api/auth/register", json={"username": "second", "password": PASSWORD}, headers=ip
    )
    assert r.status_code == 429


async def test_ac11_rate_limit_counts_per_cf_connecting_ip(auth_client):
    """取 socket 地址会让全站共用一个计数器，第一个注册者挡住所有人。"""
    for i, ip in enumerate([unique_ip(), unique_ip(), unique_ip()]):
        r = await auth_client.post(
            "/api/auth/register",
            json={"username": f"user{i}", "password": PASSWORD},
            headers={"CF-Connecting-IP": ip},
        )
        assert r.status_code == 201, f"{ip} 应当各算各的"


async def test_ac11_new_user_has_zero_credits_and_gets_402(
    auth_client, db_session_factory, monkeypatch
):
    """FR-0：注册送 0 点，能登录能浏览，但任何 LLM 功能都被 402 挡住。"""
    r = await auth_client.post(
        "/api/auth/register",
        json={"username": "broke", "password": PASSWORD},
        headers={"CF-Connecting-IP": unique_ip()},
    )
    assert r.json()["credits"] == 0

    headers = await login(auth_client, "broke")
    async with db_session_factory() as s:
        user = (await s.execute(select(User).where(User.username == "broke"))).scalar_one()
    pid = await owned_project(db_session_factory, user.id, status="script_review", shots=2)

    r = await auth_client.post(f"/api/projects/{pid}/approve-script", headers=headers)
    assert r.status_code == 402


# ── AC-5：机器令牌 ─────────────────────────────────────────────────────────

async def test_ac5_machine_token_grants_access_and_wrong_token_is_401(
    auth_client, monkeypatch
):
    monkeypatch.setattr(settings, "auth_enforced", True)
    monkeypatch.setattr(settings, "machine_token", "s3cret-machine-token")

    ok = await auth_client.get(
        "/api/projects", headers={"Authorization": "Bearer s3cret-machine-token"}
    )
    assert ok.status_code == 200

    bad = await auth_client.get("/api/projects", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401


async def test_machine_token_bound_to_user_is_scoped_and_billable(
    auth_client, db_session_factory, monkeypatch
):
    """绑定账号后，机器调用与该用户同权同计费（不再是免计费通道）。"""
    uid = await make_user(db_session_factory, "botuser", credits_=1000)
    monkeypatch.setattr(settings, "auth_enforced", True)
    monkeypatch.setattr(settings, "machine_token", "bound-token")
    monkeypatch.setattr(settings, "machine_token_user", "botuser")

    mine = await owned_project(db_session_factory, uid)
    other = await owned_project(db_session_factory, None)

    headers = {"Authorization": "Bearer bound-token"}
    assert (await auth_client.get(f"/api/projects/{mine}", headers=headers)).status_code == 200
    assert (await auth_client.get(f"/api/projects/{other}", headers=headers)).status_code == 404


async def test_mcp_client_sends_bearer_token(monkeypatch):
    """FR-5：MCP 从环境变量读令牌并在 BackendClient 里带上。"""
    from mcp_server import client as mcp_client
    from mcp_server.config import settings as mcp_settings

    monkeypatch.setattr(mcp_settings, "machine_token", "", raising=False)
    assert "Authorization" not in mcp_client._auth_headers()

    monkeypatch.setattr(mcp_settings, "machine_token", "tok", raising=False)
    assert mcp_client._auth_headers()["Authorization"] == "Bearer tok"


# ── AC-2.1 / AC-2.2：按用户过滤 + 防 IDOR ──────────────────────────────────

async def test_ac2_1_other_user_sees_no_projects(auth_client, db_session_factory):
    alice = await make_user(db_session_factory, "alice2")
    await make_user(db_session_factory, "bob2")
    await owned_project(db_session_factory, alice)

    a_headers = await login(auth_client, "alice2")
    b_headers = await login(auth_client, "bob2")

    assert (await auth_client.get("/api/projects", headers=a_headers)).json()["total"] == 1
    assert (await auth_client.get("/api/projects", headers=b_headers)).json()["total"] == 0


async def test_list_ignores_creator_query_param_when_logged_in(
    auth_client, db_session_factory
):
    """把过滤条件交给调用方等于没有过滤。"""
    alice = await make_user(db_session_factory, "alice3")
    bob = await make_user(db_session_factory, "bob3")
    await owned_project(db_session_factory, alice)
    await owned_project(db_session_factory, bob)

    b_headers = await login(auth_client, "bob3")
    r = await auth_client.get("/api/projects?creator=owner", headers=b_headers)
    assert r.json()["total"] == 1, "creator 参数不该能撬开别人的项目"


async def test_ac2_2_knowing_the_id_is_not_enough(auth_client, db_session_factory):
    """逐端点验证：只做列表过滤、漏掉归属校验是这类改造最典型的漏洞。"""
    alice = await make_user(db_session_factory, "alice4")
    await make_user(db_session_factory, "bob4", credits_=10_000)
    pid = await owned_project(db_session_factory, alice, status="script_review")

    b = await login(auth_client, "bob4")
    for method, path in [
        ("get", f"/api/projects/{pid}"),
        ("get", f"/api/projects/{pid}/script"),
        ("delete", f"/api/projects/{pid}"),
        ("get", f"/api/projects/{pid}/stream"),
        ("post", f"/api/projects/{pid}/start"),
        ("post", f"/api/projects/{pid}/approve-script"),
        ("post", f"/api/projects/{pid}/regenerate-script"),
        ("post", f"/api/projects/{pid}/export"),
        ("post", f"/api/projects/{pid}/shots/1/generate-tail-frame"),
        ("post", f"/api/projects/{pid}/shots/1/voice-convert"),
    ]:
        r = await getattr(auth_client, method)(path, headers=b)
        assert r.status_code == 404, f"{method.upper()} {path} 越权可达（IDOR）"


async def test_legacy_unowned_projects_stay_visible_until_enforcement(
    auth_client, db_session_factory, monkeypatch
):
    """owner_id IS NULL 是 P3 迁移前的存量数据。

    开关关闭时放行（否则 P2 一登录就是空列表）；开关打开时（迁移已完成）
    一律按严格归属处理。
    """
    await make_user(db_session_factory, "alice5")
    legacy = await owned_project(db_session_factory, None)
    headers = await login(auth_client, "alice5")

    assert (await auth_client.get("/api/projects", headers=headers)).json()["total"] == 1
    assert (await auth_client.get(f"/api/projects/{legacy}", headers=headers)).status_code == 200

    monkeypatch.setattr(settings, "auth_enforced", True)
    assert (await auth_client.get("/api/projects", headers=headers)).json()["total"] == 0
    assert (await auth_client.get(f"/api/projects/{legacy}", headers=headers)).status_code == 404


async def test_created_project_records_owner_id(auth_client, db_session_factory):
    await make_user(db_session_factory, "creator1")
    headers = await login(auth_client, "creator1")
    r = await auth_client.post(
        "/api/projects", json={"title": "T", "theme_text": "X"}, headers=headers
    )
    assert r.status_code == 201
    register_test_project(r.json()["id"])
    async with db_session_factory() as s:
        project = (await s.execute(
            select(Project).where(Project.id == r.json()["id"])
        )).scalar_one()
        user = (await s.execute(select(User).where(User.username == "creator1"))).scalar_one()
    assert project.owner_id == user.id
    assert project.creator_name == "creator1"


# ── AC-7 / AC-8：入队前扣、并发不透支 ──────────────────────────────────────

async def test_ac7_insufficient_credits_enqueues_nothing(
    auth_client, db_session_factory
):
    """只断言「返回 402」不够 —— 必须核对队列里确实没有新任务。"""
    uid = await make_user(db_session_factory, "poor", credits_=0)
    pid = await owned_project(db_session_factory, uid, status="script_review", shots=2)
    headers = await login(auth_client, "poor")

    auth_client.arq.enqueue_job.reset_mock()
    r = await auth_client.post(f"/api/projects/{pid}/approve-script", headers=headers)
    assert r.status_code == 402
    auth_client.arq.enqueue_job.assert_not_called()


async def test_402_leaves_project_state_untouched(auth_client, db_session_factory):
    """402 不能把项目留在「已推进但没任务在跑」的半路状态。"""
    uid = await make_user(db_session_factory, "poor2", credits_=0)
    pid = await owned_project(db_session_factory, uid, status="script_review", shots=2)
    headers = await login(auth_client, "poor2")

    assert (await auth_client.post(
        f"/api/projects/{pid}/approve-script", headers=headers
    )).status_code == 402

    async with db_session_factory() as s:
        project = (await s.execute(select(Project).where(Project.id == pid))).scalar_one()
    assert project.status == "script_review"


async def test_reserve_deducts_and_writes_ledger_in_one_transaction(
    auth_client, db_session_factory
):
    uid = await make_user(db_session_factory, "spender", credits_=200)
    principal = Principal(username="spender", user_id=uid)

    entry_id = await credits.reserve(principal, 120, ref_type="shot_video", ref_id="p:1")
    assert entry_id

    async with db_session_factory() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        rows = (await s.execute(
            select(CreditLedger).where(CreditLedger.user_id == uid)
        )).scalars().all()
    assert user.credits == 80
    assert len(rows) == 1
    assert rows[0].delta == -120 and rows[0].reason == "reserve"


@pytest.fixture
async def concurrent_db(tmp_path, monkeypatch):
    """按**生产形态**建库：文件 sqlite + NullPool，每个 session 一条独立连接。

    并发扣减不能用其余测试那套内存库 + StaticPool 来验：那是所有 session 共用
    **一条**连接，事务会互相串（一个 session 的 commit/rollback 直接作用到别人
    的事务上）。真正要验的是「WHERE credits >= :amount 匹配不到行」这条 DB 级
    保证，必须让并发方各自持有连接才作数。app/db.py 在 sqlite 下用的正是
    NullPool，所以这里与线上一致。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    import app.db as db_module
    from app.models.project import Base

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/concurrency.db", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_module, "AsyncSession", factory)
    yield factory
    await engine.dispose()


async def test_ac8_concurrent_requests_cannot_overdraw(concurrent_db):
    """余额只够 1 次时并发 10 个：恰好 1 个成功，9 个 402，且余额不为负。"""
    uid = await make_user(concurrent_db, "racer", credits_=120)
    principal = Principal(username="racer", user_id=uid)

    async def attempt(i):
        try:
            return await credits.reserve(
                principal, 120, ref_type="shot_video", ref_id=f"p:{i}"
            )
        except credits.InsufficientCredits:
            return None

    results = await asyncio.gather(*(attempt(i) for i in range(10)))
    assert sum(1 for r in results if r) == 1, "并发下必须恰好一个成功"

    async with concurrent_db() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        reserves = (await s.execute(
            select(CreditLedger).where(CreditLedger.reason == "reserve")
        )).scalars().all()
    assert user.credits == 0, "余额绝不能为负"
    assert len(reserves) == 1, "扣减与流水必须一一对应（同事务）"


async def test_deduction_and_ledger_survive_together_under_concurrency(concurrent_db):
    """扣了钱就必须查得到出处：净扣额与流水总和恒等。"""
    uid = await make_user(concurrent_db, "racer2", credits_=600)
    principal = Principal(username="racer2", user_id=uid)

    async def attempt(i):
        try:
            return await credits.reserve(
                principal, 120, ref_type="shot_video", ref_id=f"p:{i}"
            )
        except credits.InsufficientCredits:
            return None

    results = await asyncio.gather(*(attempt(i) for i in range(10)))
    succeeded = [r for r in results if r]
    assert len(succeeded) == 5, "600 / 120 = 恰好 5 次"

    async with concurrent_db() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        rows = (await s.execute(select(CreditLedger))).scalars().all()
    assert user.credits == 0
    assert sum(r.delta for r in rows) == -600, "流水总和必须解释掉全部扣减"
    assert len(rows) == len(succeeded)


# ── AC-9：退款，按分镜粒度且幂等 ───────────────────────────────────────────

async def test_ac9_refund_is_granular_and_idempotent(auth_client, db_session_factory):
    """5 个分镜预扣 5 份，其中 2 个失败 → 净扣 3 份；重复退款不重复退。"""
    cost = credits.video_cost(8)
    uid = await make_user(db_session_factory, "refundee", credits_=cost * 5)
    principal = Principal(username="refundee", user_id=uid)

    reservations = [
        await credits.reserve(principal, cost, ref_type="shot_video", ref_id=f"p:{i}")
        for i in range(1, 6)
    ]

    async with db_session_factory() as s:
        assert (await s.execute(select(User).where(User.id == uid))).scalar_one().credits == 0

    assert await credits.refund(reservations[1]) is True
    assert await credits.refund(reservations[3]) is True
    # 幂等：重复触发不得重复退
    assert await credits.refund(reservations[1]) is False
    assert await credits.refund(reservations[3]) is False

    async with db_session_factory() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        rows = (await s.execute(
            select(CreditLedger).where(CreditLedger.user_id == uid)
        )).scalars().all()
    assert user.credits == cost * 2, "失败几个退几个，不整单退也不整单不退"
    assert len([r for r in rows if r.reason == "reserve"]) == 5
    assert len([r for r in rows if r.reason == "refund"]) == 2


async def test_refund_of_unknown_reservation_is_a_noop(auth_client, db_session_factory):
    assert await credits.refund(None) is False
    assert await credits.refund("no-such-reservation") is False


async def test_worker_refunds_when_there_is_nothing_to_generate(
    auth_client, db_session_factory, redis
):
    """预扣了却一个分镜都没生成 —— 预扣必须原样退回。"""
    from worker import tasks

    uid = await make_user(db_session_factory, "workeruser", credits_=500)
    principal = Principal(username="workeruser", user_id=uid)
    pid = await owned_project(db_session_factory, uid, status="shot_generating")
    reservation = await credits.reserve(
        principal, credits.video_cost(8), ref_type="shot_video", ref_id=f"{pid}:1"
    )

    ctx = {"session_factory": db_session_factory, "redis": redis}
    await tasks.run_shot_pipeline(ctx, pid, "user:workeruser", reservation_id=reservation)

    async with db_session_factory() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalar_one()
    assert user.credits == 500


# ── AC-10：管理员发放 ──────────────────────────────────────────────────────

async def test_ac10_admin_can_grant_and_ledger_shows_it(auth_client, db_session_factory):
    await make_user(db_session_factory, "stella_test", is_admin=True)
    await make_user(db_session_factory, "grantee")
    headers = await login(auth_client, "stella_test")

    r = await auth_client.post(
        "/api/admin/users/grantee/credits",
        json={"delta": 500, "reason": "定向投喂"},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["credits"] == 500

    ledger = await auth_client.get("/api/admin/users/grantee/ledger", headers=headers)
    assert [e["reason"] for e in ledger.json()] == ["grant"]
    assert ledger.json()[0]["delta"] == 500


async def test_ac10_non_admin_gets_403(auth_client, db_session_factory):
    await make_user(db_session_factory, "plainuser")
    await make_user(db_session_factory, "victim")
    headers = await login(auth_client, "plainuser")

    r = await auth_client.post(
        "/api/admin/users/victim/credits", json={"delta": 999999}, headers=headers
    )
    assert r.status_code == 403


async def test_admin_reclaim_does_not_drive_balance_negative(
    auth_client, db_session_factory
):
    await make_user(db_session_factory, "admin2", is_admin=True)
    await make_user(db_session_factory, "smallbalance", credits_=30)
    headers = await login(auth_client, "admin2")

    r = await auth_client.post(
        "/api/admin/users/smallbalance/credits", json={"delta": -100}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["credits"] == 0


async def test_ac12_500_credits_covers_four_eight_second_shots(
    auth_client, db_session_factory
):
    """定向投喂基准量的端到端口径校验：500 点跑完剧本+分镜表+4 个 8 秒分镜不 402。"""
    uid = await make_user(db_session_factory, "budget", credits_=500)
    principal = Principal(username="budget", user_id=uid)

    assert await credits.reserve(principal, credits.script_cost(), ref_type="script", ref_id="p")
    assert await credits.reserve(principal, credits.shotlist_cost(), ref_type="shotlist", ref_id="p")
    for i in range(4):
        assert await credits.reserve(
            principal, credits.video_cost(8), ref_type="shot_video", ref_id=f"p:{i}"
        )

    async with db_session_factory() as s:
        user = (await s.execute(select(User).where(User.id == uid))).scalar_one()
    assert user.credits == 10, "跑完 4 个 8 秒分镜后应剩下少量余额"


# ── 匿名路径（P1 线上态）不受影响 ──────────────────────────────────────────

async def test_anonymous_path_is_unchanged_when_not_enforced(
    auth_client, db_session_factory
):
    """P1 的判据：AUTH_ENFORCED=false 时线上行为与鉴权上线前完全一致。"""
    pid = await owned_project(db_session_factory, None, status="script_review", shots=2)
    auth_client.arq.enqueue_job.reset_mock()

    r = await auth_client.post(
        f"/api/projects/{pid}/approve-script", headers={"X-User-Name": "anonymous"}
    )
    assert r.status_code == 202
    # 未计费路径的入队调用与鉴权上线前逐字相同 —— 不带 reservation_id
    auth_client.arq.enqueue_job.assert_called_once_with(
        "run_shot_pipeline", pid, "user:anonymous"
    )

    async with db_session_factory() as s:
        assert (await s.execute(select(CreditLedger))).scalars().all() == []
