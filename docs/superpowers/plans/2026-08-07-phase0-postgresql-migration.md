# 第 0 期：迁移到 PostgreSQL — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把后端数据库从 SQLite 迁移到 PostgreSQL，并用 Alembic 取代手写的建表/建列逻辑，为后续计费账本提供真正的事务与行级锁能力。

**Architecture:** 分三层推进：先让代码「能连 PG」（依赖 + 连接池 + URL 组装），再让「表结构由 Alembic 管」（initial revision 取代 `create_all` + `_ensure_columns`），最后让「运行环境切过去」（compose 起 PG + 测试套件切 PG + 存量数据搬迁）。全程**不改任何业务逻辑**，因此每一步都可独立回滚。

**Tech Stack:** Python 3.12、SQLAlchemy 2.0（async）、asyncpg、Alembic、PostgreSQL 16、pytest + pytest-asyncio、podman compose

## Global Constraints

- **绝不直接调用 `python` / `python3` / `pip`**。Python 一律用 `uv run --project backend ...`，装包一律改 `backend/pyproject.toml` 后 `uv sync --project backend`。
- **后端测试直接在宿主机跑** `uv run --project backend pytest`，不要绕 podman。
- **不写死绝对路径**。Python 里用 `Path(__file__)` 派生，compose 里用相对路径。
- **不改任何业务逻辑**。本期只动数据库接入层、表结构管理方式、测试基建与部署配置。任何对 `app/api/`、`app/agents/`、`app/services/`、`worker/` 中业务行为的改动都超出范围。
- **密钥走 K8s 风格流程**：新增的 `postgres_password` 必须加进 `deploy/secrets.yml.example`（占位值）、`secrets.yml`（真值，gitignored）、compose 的 `secrets:` 段。**禁止**出现在代码、`config.yml` 或镜像里。
- **每个任务结束都要提交**。提交信息用中文，说明「做了什么 + 为什么」。
- 数据库表结构的唯一真相是 `backend/app/models/project.py` 里的 SQLAlchemy 模型；Alembic revision 必须与之一致。

---

## 背景：为什么这件事没法「先凑合」

新人容易以为换数据库就是改一行 `DATABASE_URL`。这里不是。当前代码有三处**硬阻断**：

1. `backend/app/db.py` 的 `_ensure_columns()` 用 **SQLite 专有的 `PRAGMA table_info`** 探测列。`PRAGMA` 不是合法的 PostgreSQL 语句，连上去**当场报错**。
2. 同一函数里的 DDL 是 SQLite 方言（`BOOLEAN NOT NULL DEFAULT 0`、连续 `DROP COLUMN`）。
3. 项目**完全没有 Alembic**，建表靠 `Base.metadata.create_all()`，改列靠上面那个手写函数。没有版本、没有回滚、没有 head 概念。

所以本期真正的工作量在「引入 Alembic + 拆掉 `_ensure_columns`」，换引擎反而是顺带的。

### 会被打断的三个调用方（务必都处理）

删 `_ensure_columns()` 会连带打断这三处，**不是可选项**：

| 调用方 | 位置 | 处理方式 |
|--------|------|----------|
| 应用启动 | `backend/app/db.py:42`（`init_db()` 内） | 改为校验 Alembic head（Task 3） |
| COS 迁移脚本 | `backend/app/scripts/cos_migration/runner.py:229,233` | 删掉调用，列已由 initial revision 建出（Task 3） |
| 幂等性测试 | `backend/tests/integration/test_shot_key_columns.py:42,48-49` | 删除该测试，其职责由 Alembic 承接（Task 3） |

---

## File Structure

| 文件 | 职责 | 动作 |
|------|------|------|
| `backend/pyproject.toml` | 依赖声明 | 修改：加 `asyncpg`、`alembic` |
| `backend/app/config.py` | 配置 | 修改：加 `postgres_*` 字段 + `resolved_database_url` 属性 + 连接池参数 |
| `backend/app/db.py` | 引擎/会话/启动校验 | 修改：加 `build_pool_kwargs()`；**删除 `_ensure_columns()`**；`init_db()` 改为校验 head |
| `backend/alembic.ini` | Alembic 配置 | 新建 |
| `backend/alembic/env.py` | Alembic async 运行环境 | 新建 |
| `backend/alembic/versions/<rev>_initial_schema.py` | 初始表结构 | 新建（自动生成后人工核对） |
| `backend/app/scripts/cos_migration/runner.py` | 历史 COS 迁移脚本 | 修改：移除 `_ensure_columns` 调用 |
| `backend/app/scripts/pg_migration/migrate.py` | SQLite→PG 数据搬迁 | 新建 |
| `backend/tests/conftest.py` | 全局测试 DB 基建 | 新建：集中提供 PG 测试引擎 |
| `backend/tests/integration/conftest.py` | 集成测试夹具 | 修改：引擎改用全局夹具 |
| `backend/tests/mcp/conftest.py` | MCP 测试夹具 | 修改：同上 |
| `backend/tests/unit/test_project_voice_fields.py` | 单测 | 修改：引擎改用全局夹具 |
| `backend/tests/unit/test_image_candidate_model.py` | 单测 | 修改：同上 |
| `backend/tests/integration/test_shot_key_columns.py` | `_ensure_columns` 幂等性测试 | **删除** |
| `deploy/docker-compose.dev.yml` | 部署编排 | 修改：加 `postgres` 服务 + `app-pgdata` 卷 + `x-db-env` 锚点 |
| `deploy/secrets.yml.example` | 密钥模板 | 修改：加 `postgres_password` 占位 |

---

## Task 1: 让代码能连上 PostgreSQL

装依赖、把数据库 URL 的组装收敛到一处、按后端类型选连接池。这一任务结束后代码**具备**连 PG 的能力，但还没真正切过去。

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/config.py`
- Modify: `backend/app/db.py:1-25`
- Test: `backend/tests/unit/test_db_pool_config.py`（新建）

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  - `app.config.Settings.resolved_database_url -> str` — 供 `db.py` 与 Alembic `env.py` 取用的**唯一**数据库 URL 来源
  - `app.db.build_pool_kwargs(database_url: str) -> dict` — 按 URL 前缀返回 `create_async_engine` 的连接池参数

- [ ] **Step 1: 加依赖**

编辑 `backend/pyproject.toml`，在 `dependencies` 列表里加两行（保持字母无关的既有排列风格，追加在 `cos-python-sdk-v5` 之后）：

```toml
    "cos-python-sdk-v5>=1.9",
    "asyncpg>=0.29",
    "alembic>=1.13",
```

然后同步：

```bash
uv sync --project backend
```

- [ ] **Step 2: 写失败的测试**

新建 `backend/tests/unit/test_db_pool_config.py`：

```python
"""连接池选择逻辑：SQLite 用 NullPool，PostgreSQL 用带尺寸的 QueuePool。

背景：aiosqlite 的每个连接是独立的异步进程，池化没有收益，且 SSE 长连接
并发时 QueuePool 会被耗尽（见 db.py 历史注释）。PostgreSQL 相反——建连接
昂贵，必须池化。所以这里按 URL 前缀分流。
"""

from sqlalchemy.pool import NullPool

from app.config import settings
from app.db import build_pool_kwargs


def test_sqlite_url_uses_nullpool():
    kwargs = build_pool_kwargs("sqlite+aiosqlite:///./metadata.db")
    assert kwargs["poolclass"] is NullPool


def test_postgres_url_uses_sized_pool():
    kwargs = build_pool_kwargs("postgresql+asyncpg://u:p@h:5432/db")
    # 不指定 poolclass —— 让 SQLAlchemy 用默认的 QueuePool
    assert "poolclass" not in kwargs
    assert kwargs["pool_size"] == settings.db_pool_size
    assert kwargs["max_overflow"] == settings.db_max_overflow
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == settings.db_pool_recycle_sec


def test_resolved_url_falls_back_to_database_url_when_no_pg_host(monkeypatch):
    monkeypatch.setattr(settings, "postgres_host", "")
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./x.db")
    assert settings.resolved_database_url == "sqlite+aiosqlite:///./x.db"


def test_resolved_url_assembles_pg_url_from_parts(monkeypatch):
    """断言的是「URL 能原样解析回各个分量」，而不是某个具体的转义字符串。

    密码里的 @ 和空格若转义错了，URL 会在 userinfo 处断错位——但不同转义
    方案（%20 vs +）都可能是对的，写死字符串会变成脆断言。回环解析才是
    真正要保证的性质。
    """
    from sqlalchemy import make_url

    monkeypatch.setattr(settings, "postgres_host", "db.example")
    monkeypatch.setattr(settings, "postgres_port", 5432)
    monkeypatch.setattr(settings, "postgres_db", "videomaker")
    monkeypatch.setattr(settings, "postgres_user", "vm")
    monkeypatch.setattr(settings, "postgres_password", "p@ss word")
    monkeypatch.setattr(settings, "postgres_password_file", "")

    url = make_url(settings.resolved_database_url)
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "vm"
    assert url.password == "p@ss word"
    assert url.host == "db.example"
    assert url.port == 5432
    assert url.database == "videomaker"


def test_resolved_url_reads_password_from_file(monkeypatch, tmp_path):
    from sqlalchemy import make_url

    pw_file = tmp_path / "postgres_password"
    pw_file.write_text("filesecret\n")  # 末尾换行必须被 strip 掉
    monkeypatch.setattr(settings, "postgres_host", "db.example")
    monkeypatch.setattr(settings, "postgres_port", 5432)
    monkeypatch.setattr(settings, "postgres_db", "videomaker")
    monkeypatch.setattr(settings, "postgres_user", "vm")
    monkeypatch.setattr(settings, "postgres_password", "")
    monkeypatch.setattr(settings, "postgres_password_file", str(pw_file))

    assert make_url(settings.resolved_database_url).password == "filesecret"
```

- [ ] **Step 3: 运行测试确认失败**

```bash
uv run --project backend pytest tests/unit/test_db_pool_config.py -v
```

预期：`ImportError: cannot import name 'build_pool_kwargs' from 'app.db'`

- [ ] **Step 4: 给 config.py 加 PostgreSQL 字段与 URL 组装**

编辑 `backend/app/config.py`。把文件顶部的 import 改成：

```python
"""Application configuration using pydantic-settings."""

from pathlib import Path
from typing import Optional

from sqlalchemy import URL
from pydantic_settings import BaseSettings
```

然后把 `database_url` 那一段（原第 22-23 行）替换为：

```python
    # Database ——「唯一真相」是 resolved_database_url 属性，不要直接读这两组字段。
    #
    # database_url 是回退值（本地开发 / 回滚到 SQLite 时用）。
    # postgres_* 一旦设了 host，就优先按分量拼出 PostgreSQL URL —— 这样
    # compose 里只需要用一个 YAML 锚点喂分量，不必在 3 个服务里各写一遍完整
    # URL（那正是迁移前的漂移来源）。
    database_url: str = "sqlite+aiosqlite:///./metadata.db"

    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_db: str = "videomaker"
    postgres_user: str = "videomaker"
    postgres_password: str = ""
    # 容器里密码以文件形式挂载（compose secrets），优先级低于 postgres_password
    postgres_password_file: str = ""

    # PostgreSQL 连接池（sqlite 分支不使用这些值）
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_sec: int = 1800

    @property
    def resolved_database_url(self) -> str:
        """应用与 Alembic 都应当只读这个属性，不要直接读 database_url。

        用 URL.create 而不是 f-string 拼接：密码里若含 @ / : / 空格，手工
        转义极易和 SQLAlchemy 的反向解析对不上（quote_plus 把空格编成 +，
        但 URL 解析 userinfo 时用的是 unquote，不会把 + 还原成空格）。
        交给 URL.create 就没有这类不对称问题。
        """
        if not self.postgres_host:
            return self.database_url
        password = self.postgres_password
        if not password and self.postgres_password_file:
            password = Path(self.postgres_password_file).read_text().strip()
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)
```

- [ ] **Step 5: 给 db.py 加 build_pool_kwargs 并改用 resolved_database_url**

编辑 `backend/app/db.py`，把文件开头到 `engine = ...` 那一段（原第 1-25 行）替换为：

```python
"""Database connection and session management."""

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import settings


def build_pool_kwargs(database_url: str) -> dict:
    """按数据库后端选择连接池策略。

    SQLite + aiosqlite：每个连接是独立的异步进程，池化无收益；而 QueuePool
    在 SSE 长连接并发时会被耗尽。用 NullPool——每次会话新建连接、释放即关。

    PostgreSQL：建连接昂贵，必须池化。pool_pre_ping 让被中间件掐掉的死连接
    在使用前被发现并重建；pool_recycle 避免连接活得比服务端 idle 超时更久。
    """
    if database_url.startswith("sqlite"):
        from sqlalchemy.pool import NullPool
        return {"poolclass": NullPool}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle_sec,
    }


_database_url = settings.resolved_database_url

# Create async engine
engine = create_async_engine(
    _database_url,
    echo=False,
    future=True,
    **build_pool_kwargs(_database_url),
)
```

> **注意**：这里**保留**了 SQLite 分支，没有按 PRD §12.1 的字面表述删掉它。理由是 PRD 同一节写明的回滚方案是「`DATABASE_URL` 切回即可回退」——若删掉 SQLite 分支，回退后会用 QueuePool 跑 aiosqlite，重现历史上打爆连接池的问题，回滚路径就废了。保留分支的成本是 6 行代码。

- [ ] **Step 6: 运行测试确认通过**

```bash
uv run --project backend pytest tests/unit/test_db_pool_config.py -v
```

预期：5 个测试全部 PASS

- [ ] **Step 7: 确认没有破坏现有测试**

```bash
uv run --project backend pytest tests/unit -q
```

预期：全部通过（此时其余测试仍跑在 SQLite 上，不受影响）

- [ ] **Step 8: 提交**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app/config.py backend/app/db.py backend/tests/unit/test_db_pool_config.py
git commit -m "feat(db): 加 asyncpg/alembic 依赖，URL 组装收敛到 resolved_database_url

compose 里 3 个服务各写一遍完整 DATABASE_URL 是漂移来源，改为按
postgres_* 分量拼装，compose 只需喂一个 YAML 锚点。

连接池按后端分流：sqlite 保留 NullPool（aiosqlite 池化无收益且会被
SSE 长连接打爆），postgres 用带尺寸的 QueuePool + pre_ping + recycle。
保留 sqlite 分支是为了让 PRD 里「切回 DATABASE_URL 即回滚」这条路可用。"
```

---

## Task 2: 引入 Alembic 并生成 initial revision

让 Alembic 接管表结构。本任务结束后，`alembic upgrade head` 能在一个空的 PostgreSQL 库里建出与当前 ORM 模型完全一致的全部表。

**Files:**
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/script.py.mako`
- Create: `backend/alembic/versions/<rev>_initial_schema.py`
- Test: `backend/tests/integration/test_alembic_schema.py`（新建）

**Interfaces:**
- Consumes: `app.config.Settings.resolved_database_url`（Task 1）
- Produces:
  - `backend/alembic.ini` — Alembic 配置文件路径，被 Task 3 的 head 校验读取
  - initial revision，包含 `projects`、`shots`、`reference_images`、`image_candidates`、`events`、`content_analyses`、`reference_samples` 全部表

- [ ] **Step 1: 起一个本地 PostgreSQL 供开发与测试用**

先把容器跑起来（Task 5 会把它正式写进 compose，这里先手起一个同名同端口的，避免两次搬迁）：

```bash
podman run -d --name video-maker-postgres-dev \
    -p 5433:5432 \
    -e POSTGRES_USER=videomaker \
    -e POSTGRES_PASSWORD=devpassword \
    -e POSTGRES_DB=videomaker \
    docker.io/library/postgres:16-alpine
```

等它就绪并建出测试库：

```bash
until podman exec video-maker-postgres-dev pg_isready -U videomaker; do sleep 1; done
podman exec video-maker-postgres-dev createdb -U videomaker videomaker_test
```

- [ ] **Step 2: 初始化 Alembic 骨架**

```bash
cd backend && uv run --project . alembic init -t async alembic
```

这会生成 `backend/alembic.ini`、`backend/alembic/env.py`、`backend/alembic/script.py.mako` 和空的 `versions/` 目录。

- [ ] **Step 3: 改 alembic.ini，去掉写死的 URL**

编辑 `backend/alembic.ini`，找到 `sqlalchemy.url = driver://user:pass@localhost/dbname` 这一行，改成空值（URL 由 `env.py` 在运行时从 settings 注入）：

```ini
sqlalchemy.url =
```

- [ ] **Step 4: 替换 env.py**

把 `backend/alembic/env.py` 整个文件替换为：

```python
"""Alembic 运行环境（async）。

数据库 URL 默认从 app.config.settings.resolved_database_url 取——保证应用与迁移
永远指向同一个库，不会出现「应用连 A、迁移改 B」。但调用方（测试、将来可能的
按租户迁移运行器）可以在调 command.upgrade() 前显式设 Config 的 sqlalchemy.url
来覆盖，走标准 Alembic 方式，不必依赖环境变量。
"""

import asyncio
from logging.config import fileConfig
from pathlib import Path
import sys

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 让 `alembic` 命令在 backend/ 目录下也能 import 到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.models.project import Base  # noqa: E402

config = context.config

# 只在 URL 尚未设置时才回退到 settings。
#
# 不能无条件覆盖：那会丢弃调用方在 command.upgrade() 之前以编程方式设到
# Config 上的 URL（测试 fixture 正是这么做的），导致迁移打到非预期的库上，
# 而「迁移了哪个库」变成隐式依赖进程环境变量——是个没写在任何地方的约定。
# 正常 CLI 路径下 alembic.ini 的 sqlalchemy.url 为空，回退照常生效。
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.resolved_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # 让 autogenerate 能发现列类型变化，否则改类型不会被检测到
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 5: 生成 initial revision**

指向本地 PG（此时是空库），让 Alembic 按 ORM 模型自动生成：

```bash
cd backend && POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
    POSTGRES_USER=videomaker POSTGRES_PASSWORD=devpassword POSTGRES_DB=videomaker \
    uv run --project . alembic revision --autogenerate -m "initial schema"
```

- [ ] **Step 6: 人工核对生成的 revision**

打开 `backend/alembic/versions/<rev>_initial_schema.py`，逐项确认：

- 7 张表都在：`projects`、`shots`、`reference_images`、`image_candidates`、`events`、`content_analyses`、`reference_samples`
- `shots`、`events`、`reference_samples` 的整数主键是 `sa.Integer` + `autoincrement=True`（PG 下会建成 `SERIAL`/identity）
- 外键的 `ondelete="CASCADE"` 都在
- `projects` 上有 `ix_projects_status`、`ix_projects_creator_name`、`ix_projects_created_at`
- `shots` 上有 `uq_shot_project_shot_id` 唯一约束

如有缺失，说明模型里有 Alembic 没识别的东西，**手工补进 revision**，不要跳过。

- [ ] **Step 7: 写测试——验证 upgrade 后的表结构与 ORM 一致**

新建 `backend/tests/integration/test_alembic_schema.py`：

```python
"""Alembic 的 initial revision 必须与 ORM 模型完全一致。

这是本期最重要的一道防线：一旦 revision 与模型漂移，应用会连上一个
「看起来能跑、但少一列」的库，故障会推迟到运行时才暴露。
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.models.project import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "projects",
    "shots",
    "reference_images",
    "image_candidates",
    "events",
    "content_analyses",
    "reference_samples",
}


def _sync_test_url() -> str:
    """Alembic 的 command API 是同步的，这里要 psycopg 风格的同步 URL。"""
    return os.environ.get(
        "TEST_DATABASE_URL_SYNC",
        "postgresql://videomaker:devpassword@localhost:5433/videomaker_test",
    )


@pytest.fixture
def alembic_config():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _sync_test_url())
    return cfg


@pytest.fixture
def clean_db(alembic_config):
    """每个测试从一个干净的 public schema 开始。"""
    engine = create_engine(_sync_test_url())
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_upgrade_head_creates_all_tables(alembic_config, clean_db):
    command.upgrade(alembic_config, "head")
    with clean_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )).scalars().all()
    assert EXPECTED_TABLES.issubset(set(rows))


def test_head_schema_matches_orm_models(alembic_config, clean_db):
    """upgrade 到 head 后，autogenerate 应当检测不出任何差异。"""
    command.upgrade(alembic_config, "head")
    with clean_db.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"Alembic revision 与 ORM 模型不一致：{diff}"
```

- [ ] **Step 8: 运行测试**

```bash
uv run --project backend pytest tests/integration/test_alembic_schema.py -v
```

预期：2 个测试 PASS。若 `test_head_schema_matches_orm_models` 报出 diff，按 diff 内容修 revision，直到为空。

- [ ] **Step 9: 提交**

```bash
git add backend/alembic.ini backend/alembic backend/tests/integration/test_alembic_schema.py
git commit -m "feat(db): 引入 Alembic 并生成 initial revision

env.py 从 settings.resolved_database_url 取 URL 而非 alembic.ini，
保证应用与迁移永远指向同一个库。

新增 test_alembic_schema 作为防漂移闸门：upgrade 到 head 后用
compare_metadata 断言 autogenerate 检测不出任何差异——revision 与 ORM
一旦分叉，CI 立刻红。"
```

---

## Task 3: 拆掉 create_all 与 _ensure_columns

把表结构管理权从手写代码彻底交给 Alembic，并处理三个被打断的调用方。

**Files:**
- Modify: `backend/app/db.py:37-…`（`init_db()` 与整个 `_ensure_columns()`）
- Modify: `backend/app/scripts/cos_migration/runner.py:216-235`
- Delete: `backend/tests/integration/test_shot_key_columns.py`
- Test: `backend/tests/integration/test_init_db_head_check.py`（新建）

**Interfaces:**
- Consumes: `backend/alembic.ini` 与 initial revision（Task 2）
- Produces:
  - `app.db.init_db() -> None` — 不再建表，改为「校验数据库已升级到 Alembic head，否则抛 `RuntimeError`」

- [ ] **Step 1: 写失败的测试**

新建 `backend/tests/integration/test_init_db_head_check.py`：

```python
"""init_db 不再建表，改为守门：库没升到 head 就拒绝启动。

这条守门很关键——迁移到 Alembic 之后，「表不存在」不再会被 create_all
悄悄兜住。宁可启动时明确报错，也不要跑到第一个查询才炸。
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import assert_migrations_current

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _sync_test_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL_SYNC",
        "postgresql://videomaker:devpassword@localhost:5433/videomaker_test",
    )


def _async_test_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://videomaker:devpassword@localhost:5433/videomaker_test",
    )


@pytest.fixture
def alembic_config():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _sync_test_url())
    return cfg


@pytest.fixture
def empty_db():
    engine = create_engine(_sync_test_url())
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield
    engine.dispose()


async def test_raises_when_db_not_migrated(empty_db):
    engine = create_async_engine(_async_test_url())
    try:
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            await assert_migrations_current(engine)
    finally:
        await engine.dispose()


async def test_passes_when_db_at_head(empty_db, alembic_config):
    command.upgrade(alembic_config, "head")
    engine = create_async_engine(_async_test_url())
    try:
        await assert_migrations_current(engine)  # 不抛异常即通过
    finally:
        await engine.dispose()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run --project backend pytest tests/integration/test_init_db_head_check.py -v
```

预期：`ImportError: cannot import name 'assert_migrations_current' from 'app.db'`

- [ ] **Step 3: 重写 db.py 的 init_db，删掉 _ensure_columns**

编辑 `backend/app/db.py`。把 `async def init_db():` 开始、一直到文件末尾（即 `init_db` 与整个 `_ensure_columns`）**全部删除**，替换为：

```python
async def assert_migrations_current(target_engine) -> None:
    """校验数据库已升级到 Alembic head，否则抛 RuntimeError。

    迁移到 Alembic 之后，表结构不再由应用启动时创建。若库落后于代码，
    我们要在启动瞬间就明确失败，而不是等到第一个查询报 UndefinedColumn。
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    backend_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    expected = set(ScriptDirectory.from_config(cfg).get_heads())

    def _current_heads(sync_conn) -> set:
        return set(MigrationContext.configure(sync_conn).get_current_heads())

    async with target_engine.connect() as conn:
        actual = await conn.run_sync(_current_heads)

    if actual != expected:
        raise RuntimeError(
            f"数据库表结构版本不匹配：库在 {actual or '{}'}，代码要求 {expected}。"
            f"请先运行 `alembic upgrade head` 再启动服务。"
        )


async def init_db():
    """启动检查。**不再建表** —— 表结构由 Alembic 管理。"""
    await assert_migrations_current(engine)
```

同时在文件顶部的 import 区加上 `Path`：

```python
"""Database connection and session management."""

from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import settings
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run --project backend pytest tests/integration/test_init_db_head_check.py -v
```

预期：2 个测试 PASS

- [ ] **Step 5: 修 cos_migration/runner.py 的调用方**

编辑 `backend/app/scripts/cos_migration/runner.py`。把 `backfill()` 函数的 docstring 中关于 `_ensure_columns` 的那段说明（原第 221-227 行）以及函数体开头的调用（原第 229-234 行）删掉。

原来的：

```python
    需要停写窗口。开始前先跑一次幂等建列 —— 切换顺序里本步骤(第 5 步)早于
    部署新代码后的首次启动(第 6 步)，而两个 key 列正是在启动时由幂等
    ALTER TABLE 创建的；不先建列这一步会直接失败（Spec B §9.2）。

    注意这里用 ``_ensure_columns(conn)`` 而**不是** ``init_db()``：后者写死
    操作 app.db 模块级的 engine（指向真实 dev.db），在测试里会绕过传进来的
    session_factory 去动共享库 —— 那正是本计划明令禁止的事。``_ensure_columns``
    接收调用方的 conn，因此永远作用在正确的引擎上（Spec A 写它时就预留了
    这个用法，见其 docstring）。该例程幂等，重复执行无害。
    """
    from app.db import _ensure_columns

    async with session_factory() as s:
        conn = await s.connection()
        await _ensure_columns(conn)
        await s.commit()

    changed = skipped = 0
```

改为：

```python
    需要停写窗口。

    历史说明：本函数曾在开头调用 ``app.db._ensure_columns`` 做幂等建列，
    因为当时 ``pre_cc_last_frame_key`` / ``pristine_last_frame_key`` 两列是
    靠应用启动时的 ALTER TABLE 创建的。迁移到 Alembic 之后，这两列由
    initial revision 建出，调用方在跑本脚本前必然已经 ``alembic upgrade head``，
    故建列步骤已删除。
    """
    changed = skipped = 0
```

- [ ] **Step 6: 删除已被 Alembic 取代的幂等性测试**

`backend/tests/integration/test_shot_key_columns.py` 专门测 `_ensure_columns` 重复执行无害。该函数已不存在，其职责（表结构可重复安全应用）现在由 Alembic 的 revision 机制与 Task 2 的 `test_alembic_schema.py` 承接。

```bash
git rm backend/tests/integration/test_shot_key_columns.py
```

- [ ] **Step 7: 确认代码库里再无 _ensure_columns 与 create_all 的生产调用**

```bash
grep -rn "_ensure_columns" backend --include=*.py | grep -v __pycache__
```

预期：无输出。

```bash
grep -rn "create_all" backend --include=*.py | grep -v __pycache__
```

预期：只剩测试文件里的引用（Task 4 会处理）。`backend/app/` 下必须一处都没有。

- [ ] **Step 8: 提交**

```bash
git add -A backend/app/db.py backend/app/scripts/cos_migration/runner.py backend/tests
git commit -m "refactor(db)!: 表结构管理权交给 Alembic，删除 _ensure_columns

_ensure_columns 用 SQLite 专有的 PRAGMA table_info 探测列，连 PostgreSQL
会当场报错——这是迁移的硬阻断，不是兼容性问题。

init_db 不再建表，改为校验库已升到 Alembic head，否则拒绝启动。迁移后
「表不存在」不再被 create_all 悄悄兜住，宁可启动时明确失败。

同步处理两个被打断的调用方：cos_migration/runner.py 删掉建列调用（列已由
initial revision 建出）；test_shot_key_columns.py 删除（其幂等性职责由
Alembic revision 机制承接）。"
```

---

## Task 4: 测试套件切到 PostgreSQL

当前 4 处测试各自 `create_async_engine("sqlite+aiosqlite:///:memory:")`，跑的根本不是 PG。PRD 的验收要求「全部后端测试在 PostgreSQL 上通过」，必须把测试基建换掉。

**Files:**
- Create: `backend/tests/conftest.py`
- Modify: `backend/tests/integration/conftest.py:91-103`
- Modify: `backend/tests/mcp/conftest.py:20-30`
- Modify: `backend/tests/unit/test_project_voice_fields.py:9-18`
- Modify: `backend/tests/unit/test_image_candidate_model.py:11-20`

**Interfaces:**
- Consumes: `app.models.project.Base`
- Produces:
  - `tests.conftest.test_database_url() -> str` — 测试库的 async URL
  - pytest fixture `db_engine` — 已建好全部表的 PostgreSQL AsyncEngine，每个测试独享干净数据

- [ ] **Step 1: 新建全局 conftest，集中提供 PG 测试引擎**

新建 `backend/tests/conftest.py`：

```python
"""全局测试基建：所有测试跑在真实 PostgreSQL 上。

为什么不用 SQLite 内存库：计费账本依赖 PostgreSQL 的行级锁与事务语义，
而 SQLite 没有。测试若跑在 SQLite 上，就测不到我们真正部署的行为——
迁移的意义有一半在这里。

隔离策略：每个测试前 drop_all + create_all。表只有 7 张，成本约 50ms，
换来的是与生产完全一致的方言行为和零残留。

为什么测试用 create_all 而生产用 Alembic：两者都以 Base.metadata 为准，
而 test_alembic_schema.py 用 compare_metadata 断言「Alembic head == ORM
metadata」。有那道闸门在，create_all 建出的表就等价于 Alembic 建出的表，
测试因此既快又不会与生产漂移。闸门一旦被删，这个等价就不成立了。
"""

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.project import Base

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://videomaker:devpassword@localhost:5433/videomaker_test"
)


def test_database_url() -> str:
    """测试库 URL。用独立的 videomaker_test 库，绝不碰开发库 videomaker。"""
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest.fixture
async def db_engine():
    engine = create_async_engine(test_database_url(), poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
```

- [ ] **Step 2: 让 integration/conftest.py 复用全局夹具**

编辑 `backend/tests/integration/conftest.py`。

**(a)** 删除第 91-107 行的这两个夹具定义（整段删掉，pytest 会自动继承 `tests/conftest.py` 里的同名夹具）：

```python
@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
```

**(b)** 删除第 9-10 行这两行 import（`create_async_engine`、`async_sessionmaker`、`StaticPool` 在本文件中只被上面删掉的夹具用到，已核实）：

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
```

**(c)** 把第 12 行去掉 `Base`（同样只被删掉的夹具用到）：

```python
from app.models.project import Project, Shot, ReferenceImage, ReferenceSample
```

- [ ] **Step 3: 让 mcp/conftest.py 复用全局夹具**

编辑 `backend/tests/mcp/conftest.py`。

**(a)** 删除第 20-35 行的这两个夹具定义：

```python
@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
```

**(b)** 删除第 5-6 行：

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
```

**(c)** 把第 8 行去掉 `Base`：

```python
from app.models.project import Project, Shot
```

**(d)** 把文件首行的 docstring 改掉，它现在描述的是已经不存在的事实：

```python
"""Fixtures for MCP server tests: real backend ASGI app over PostgreSQL."""
```

- [ ] **Step 4: 改两个单测文件**

两个文件都定义了一个叫 `sf` 的本地夹具，自建 SQLite 引擎。最小改动是**保留 `sf` 这个名字**、改为委托给全局的 `db_session_factory` —— 这样所有测试函数体一行都不用动。

编辑 `backend/tests/unit/test_project_voice_fields.py`，把开头替换为：

```python
import pytest
from sqlalchemy import select
from app.models.project import Project


@pytest.fixture
async def sf(db_session_factory):
    """沿用原名 sf，实际委托给 tests/conftest.py 的 PostgreSQL 会话工厂。"""
    return db_session_factory
```

编辑 `backend/tests/unit/test_image_candidate_model.py`，把开头替换为：

```python
"""ImageCandidate 模型 + 序列化 + candidates 目录 helper."""
import pytest
from sqlalchemy import select

from app.models.project import Project, Shot


@pytest.fixture
async def sf(db_session_factory):
    """沿用原名 sf，实际委托给 tests/conftest.py 的 PostgreSQL 会话工厂。"""
    return db_session_factory
```

两个文件的其余内容（测试函数、`_seed` helper）**保持不动**。

- [ ] **Step 5: 跑全套测试**

确保 PG 容器在跑（Task 2 Step 1 起的），然后：

```bash
uv run --project backend pytest -q
```

预期：全部通过。若出现失败，绝大多数会是这两类，逐个修：

- **`sqlite3.OperationalError` 残留** → 还有测试自建 SQLite 引擎没改到，`grep -rn "sqlite" backend/tests --include=*.py` 找出来。
- **`asyncpg.exceptions.UndefinedTableError`** → 该测试用了 `db_session_factory` 却没经过 `db_engine`，检查夹具依赖链。

- [ ] **Step 6: 确认测试目录里不再有 SQLite**

```bash
grep -rn "sqlite" backend/tests --include=*.py | grep -v __pycache__
```

预期：只剩 `e2e_seed/` 里的注释文字（那些脚本从环境变量取 URL，不写死引擎）。任何 `create_async_engine("sqlite...` 都必须已消失。

- [ ] **Step 7: 提交**

```bash
git add -A backend/tests
git commit -m "test: 测试套件从 SQLite 内存库切到真实 PostgreSQL

原先 4 处测试各自 create_async_engine('sqlite+aiosqlite:///:memory:')，
跑的根本不是我们要部署的数据库——方言差异、行级锁语义、事务行为全测不到。
后续计费账本恰恰依赖这些。

新增 tests/conftest.py 集中提供 db_engine/db_session_factory，指向独立的
videomaker_test 库（绝不碰开发库）。每测试 drop_all+create_all 保证零残留。"
```

---

## Task 5: compose 切换到 PostgreSQL

把 PG 正式写进编排，并消除 3 处硬编码 `DATABASE_URL` 的漂移源。

**Files:**
- Modify: `deploy/docker-compose.dev.yml`
- Modify: `deploy/secrets.yml.example`

**Interfaces:**
- Consumes: `app.config.Settings.postgres_*` 字段（Task 1）
- Produces: 运行中的 `video-maker-postgres-dev` 服务与 `app-pgdata` 具名卷

- [ ] **Step 1: 加密钥占位**

编辑 `deploy/secrets.yml.example`，追加一行：

```yaml
postgres_password: change-me-to-a-strong-password
```

同时在本机的 `secrets.yml`（gitignored，不提交）里加上真值，然后：

```bash
make secrets
```

- [ ] **Step 2: 在 compose 顶部加 YAML 锚点**

编辑 `deploy/docker-compose.dev.yml`，在 `services:` 之前插入：

```yaml
# 数据库连接分量 —— 三个 Python 服务共用这一处定义。
# 迁移前这里是 3 份写死的 DATABASE_URL，改一处漏两处是常态。
x-db-env: &db-env
  POSTGRES_HOST: video-maker-postgres-dev
  POSTGRES_PORT: "5432"
  POSTGRES_DB: videomaker
  POSTGRES_USER: videomaker
  POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
```

- [ ] **Step 3: 加 postgres 服务**

在 `services:` 下、`redis:` 之后插入：

```yaml
  postgres:
    image: docker.io/library/postgres:16-alpine
    container_name: video-maker-postgres-dev
    ports:
      - "5433:5432"
    environment:
      POSTGRES_USER: videomaker
      POSTGRES_DB: videomaker
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
    secrets:
      - postgres_password
    volumes:
      - app-pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U videomaker -d videomaker"]
      interval: 5s
      timeout: 3s
      retries: 12
    restart: unless-stopped
```

- [ ] **Step 4: 声明密钥与卷**

在文件底部的 `secrets:` 段加：

```yaml
  postgres_password:
    file: ./secrets/postgres_password   # written by: make secrets
```

> 路径是 `./secrets/`，与该文件里已有的 8 个 secrets 条目一致（Makefile 的
> `DEPLOY_DIR=deploy` 就是往这里写）。别写成 `../secrets/`——那是 CLAUDE.md 里
> 一段示意片段的写法，与本仓库实际约定不符，照抄会让起栈时找不到文件。

在 `volumes:` 段加：

```yaml
  app-pgdata:    # PostgreSQL 数据目录，与 app-data(sqlite) 并存以便回滚
```

- [ ] **Step 5: 改三个 Python 服务**

对 `backend`、`worker`、`vc-worker` 三个服务，各做三件事：

1. **删掉**这一行：`DATABASE_URL: sqlite+aiosqlite:////app/data/dev.db`
2. 在 `environment:` 块的**第一行**插入锚点引用：`<<: *db-env`
3. 在 `secrets:` 列表加一项：`- postgres_password`
4. 在 `depends_on:` 加上 postgres 并要求健康：

```yaml
    depends_on:
      redis:
        condition: service_started
      postgres:
        condition: service_healthy
```

以 `backend` 为例，`environment:` 改完后形如：

```yaml
    environment:
      <<: *db-env
      REDIS_URL: redis://video-maker-redis-dev:6379
      GOOGLE_APPLICATION_CREDENTIALS: /run/secrets/gcp-sa.json
      HTTP_PROXY: http://host.containers.internal:10809
      ...
```

> `app-data` 卷**保留不删**——里面是旧 SQLite 库，Task 6 要从它搬数据，也是回滚的依据。

> **关于分服务调池大小**：PRD §12.1 提到「池大小按 backend / worker / vc-worker / mcp 分别配置」。这里先让四者共用默认值（`db_pool_size=5`、`max_overflow=10`），因为目前没有任何实测数据支撑差异化取值，凭空拍不同的数字只会制造维护负担。需要时在对应服务的 `environment:` 里加 `DB_POOL_SIZE: "N"` 覆盖即可——`Settings` 已经是环境变量驱动的，不需要改代码。等 §11.4 的连接池指标上线、看到哪个服务真的吃紧，再按数据调。

- [ ] **Step 6: 起栈并跑迁移**

```bash
podman compose -f deploy/docker-compose.dev.yml up -d postgres
until podman exec video-maker-postgres-dev pg_isready -U videomaker -d videomaker; do sleep 1; done
podman compose -f deploy/docker-compose.dev.yml run --rm backend \
    uv run --project . alembic upgrade head
```

- [ ] **Step 7: 起全栈并验证**

```bash
podman compose -f deploy/docker-compose.dev.yml up -d
curl -s localhost:8002/health
```

预期：健康检查返回正常。若 backend 启动即退出并报「数据库表结构版本不匹配」，说明 Step 6 的 `alembic upgrade head` 没跑成功——这正是 Task 3 那道守门在起作用，回去重跑。

- [ ] **Step 8: 确认三个服务都连到了 PG**

```bash
podman exec video-maker-postgres-dev psql -U videomaker -d videomaker \
    -c "SELECT count(*) FROM pg_stat_activity WHERE datname='videomaker';"
```

预期：连接数 > 0。

- [ ] **Step 9: 提交**

```bash
git add deploy/docker-compose.dev.yml deploy/secrets.yml.example
git commit -m "chore(deploy): compose 切到 PostgreSQL，DATABASE_URL 收敛到 YAML 锚点

原先 backend/worker/vc-worker 三处各写一遍完整 DATABASE_URL，改一处漏两处
是常态。现在共用 x-db-env 锚点喂 POSTGRES_* 分量，URL 由 config.py 的
resolved_database_url 统一拼装。

postgres 服务带 healthcheck，三个 Python 服务 depends_on service_healthy，
避免抢在库就绪前启动。app-data(sqlite) 卷保留不删——是数据搬迁的来源，
也是回滚依据。"
```

---

## Task 6: 搬迁存量数据

把共享 SQLite 库里的开发数据搬到 PostgreSQL，并逐表比对行数。

**Files:**
- Create: `backend/app/scripts/pg_migration/__init__.py`
- Create: `backend/app/scripts/pg_migration/migrate.py`
- Test: `backend/tests/integration/test_pg_migration.py`（新建）

**Interfaces:**
- Consumes: `app.models.project.Base`
- Produces:
  - `app.scripts.pg_migration.migrate.copy_all(src_url: str, dst_url: str) -> dict[str, int]` — 按外键顺序全量搬迁，返回 `{表名: 行数}`
  - `app.scripts.pg_migration.migrate.TABLE_ORDER: tuple[str, ...]` — 满足外键依赖的搬迁顺序

- [ ] **Step 1: 写失败的测试**

新建 `backend/tests/integration/test_pg_migration.py`：

```python
"""SQLite → PostgreSQL 数据搬迁。

两个必须验证的点：
1. 按外键顺序搬，否则子表先插会违反外键约束。
2. PostgreSQL 的自增序列（SERIAL）必须在搬完后 setval 对齐，否则下一次
   INSERT 会从 1 开始，撞上已搬进来的主键——这是跨库搬迁最经典的坑。
"""

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select, text
from sqlalchemy.pool import NullPool

from app.models.project import Base, Event, Project, Shot
from app.scripts.pg_migration.migrate import TABLE_ORDER, copy_all


def _pg_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://videomaker:devpassword@localhost:5433/videomaker_test",
    )


@pytest.fixture
async def sqlite_src(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'src.db'}"
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        p = Project(
            id="11111111-1111-1111-1111-111111111111",
            title="搬迁样例", theme_text="主题", creator_name="wayne",
            status="draft", aspect_ratio="9:16",
        )
        s.add(p)
        await s.flush()
        s.add(Shot(
            project_id=p.id, shot_id=1, text="台词", shot_type="Close-up",
            visual_description="描述", shot_duration=8, status="pending",
        ))
        s.add(Event(project_id=p.id, actor="user:wayne", event_type="created"))
        await s.commit()
    yield url
    await engine.dispose()


def test_table_order_puts_parents_before_children():
    assert TABLE_ORDER.index("projects") < TABLE_ORDER.index("shots")
    assert TABLE_ORDER.index("projects") < TABLE_ORDER.index("events")
    assert TABLE_ORDER.index("shots") < TABLE_ORDER.index("image_candidates")
    assert TABLE_ORDER.index("content_analyses") < TABLE_ORDER.index("reference_samples")


async def test_copies_all_rows(sqlite_src):
    dst = create_async_engine(_pg_url(), poolclass=NullPool)
    async with dst.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await dst.dispose()

    counts = await copy_all(sqlite_src, _pg_url())
    assert counts["projects"] == 1
    assert counts["shots"] == 1
    assert counts["events"] == 1

    engine = create_async_engine(_pg_url(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        rows = (await s.execute(select(Project))).scalars().all()
        assert len(rows) == 1
        assert rows[0].title == "搬迁样例"
    await engine.dispose()


async def test_sequences_are_realigned_after_copy(sqlite_src):
    """搬完之后插新行，主键不能撞上已搬进来的。"""
    dst = create_async_engine(_pg_url(), poolclass=NullPool)
    async with dst.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await dst.dispose()

    await copy_all(sqlite_src, _pg_url())

    engine = create_async_engine(_pg_url(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # 序列没对齐的话，这一插会因主键冲突而失败
        s.add(Event(
            project_id="11111111-1111-1111-1111-111111111111",
            actor="system:worker", event_type="after_migration",
        ))
        await s.commit()
        total = (await s.execute(text("SELECT count(*) FROM events"))).scalar()
        assert total == 2
    await engine.dispose()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run --project backend pytest tests/integration/test_pg_migration.py -v
```

预期：`ModuleNotFoundError: No module named 'app.scripts.pg_migration'`

- [ ] **Step 3: 写搬迁脚本**

新建 `backend/app/scripts/pg_migration/__init__.py`（空文件）和 `backend/app/scripts/pg_migration/migrate.py`：

```python
"""把存量数据从 SQLite 全量搬到 PostgreSQL。

用法（在 backend/ 下）：

    uv run --project . python -m app.scripts.pg_migration.migrate \\
        --src sqlite+aiosqlite:////app/data/dev.db \\
        --dst postgresql+asyncpg://videomaker:PASS@localhost:5433/videomaker

前置条件：目标库已 `alembic upgrade head`（表结构必须先在）。
本脚本只插数据，不建表。
"""

import argparse
import asyncio

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models.project import Base

# 外键依赖顺序：父表必须先于子表。
# content_analyses 独立于 projects，但 projects.content_analysis_id 指向它，
# 所以放在最前面最安全。
TABLE_ORDER: tuple[str, ...] = (
    "content_analyses",
    "reference_samples",
    "projects",
    "shots",
    "reference_images",
    "image_candidates",
    "events",
)

# 用 SERIAL/identity 做主键的表 —— 搬完必须对齐序列
_SEQUENCE_TABLES: tuple[tuple[str, str], ...] = (
    ("shots", "id"),
    ("events", "id"),
    ("reference_samples", "id"),
)


async def copy_all(src_url: str, dst_url: str) -> dict[str, int]:
    """按外键顺序全量搬迁，返回每张表搬了多少行。"""
    src = create_async_engine(src_url, poolclass=NullPool)
    dst = create_async_engine(dst_url, poolclass=NullPool)
    counts: dict[str, int] = {}
    try:
        for name in TABLE_ORDER:
            table = Base.metadata.tables[name]
            async with src.connect() as sconn:
                rows = [dict(r) for r in (await sconn.execute(select(table))).mappings()]
            if rows:
                async with dst.begin() as dconn:
                    await dconn.execute(insert(table), rows)
            counts[name] = len(rows)

        # 对齐自增序列：不做的话下一次 INSERT 会从 1 开始，撞上已搬入的主键。
        async with dst.begin() as dconn:
            for table_name, pk in _SEQUENCE_TABLES:
                await dconn.execute(text(
                    f"SELECT setval("
                    f"  pg_get_serial_sequence('{table_name}', '{pk}'),"
                    f"  COALESCE((SELECT MAX({pk}) FROM {table_name}), 1),"
                    f"  (SELECT MAX({pk}) IS NOT NULL FROM {table_name})"
                    f")"
                ))
    finally:
        await src.dispose()
        await dst.dispose()
    return counts


async def verify(src_url: str, dst_url: str) -> dict[str, tuple[int, int]]:
    """逐表比对源库与目标库的行数，返回 {表名: (源, 目标)}。"""
    src = create_async_engine(src_url, poolclass=NullPool)
    dst = create_async_engine(dst_url, poolclass=NullPool)
    result: dict[str, tuple[int, int]] = {}
    try:
        for name in TABLE_ORDER:
            table = Base.metadata.tables[name]
            stmt = select(func.count()).select_from(table)
            async with src.connect() as c:
                s_n = (await c.execute(stmt)).scalar_one()
            async with dst.connect() as c:
                d_n = (await c.execute(stmt)).scalar_one()
            result[name] = (s_n, d_n)
    finally:
        await src.dispose()
        await dst.dispose()
    return result


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="源 SQLite URL")
    ap.add_argument("--dst", required=True, help="目标 PostgreSQL URL")
    args = ap.parse_args()

    counts = await copy_all(args.src, args.dst)
    for name, n in counts.items():
        print(f"  搬迁 {name}: {n} 行")

    print("\n逐表比对：")
    mismatched = False
    for name, (s_n, d_n) in (await verify(args.src, args.dst)).items():
        flag = "OK" if s_n == d_n else "不一致"
        if s_n != d_n:
            mismatched = True
        print(f"  {name}: 源={s_n} 目标={d_n} [{flag}]")

    if mismatched:
        raise SystemExit("搬迁后行数不一致，请勿切流量")
    print("\n全部一致。")


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run --project backend pytest tests/integration/test_pg_migration.py -v
```

预期：3 个测试 PASS

- [ ] **Step 5: 对真实开发数据跑一次搬迁**

先把共享卷里的 SQLite 库复制出来（**不要**直接对着它写）：

```bash
podman compose -f deploy/docker-compose.dev.yml run --rm \
    -v ./pg-migration-backup:/backup:z backend \
    cp /app/data/dev.db /backup/dev.db.bak
```

然后在容器里执行搬迁（`PGPASS` 换成 `secrets.yml` 里的真值）：

```bash
podman compose -f deploy/docker-compose.dev.yml run --rm backend \
    uv run --project . python -m app.scripts.pg_migration.migrate \
    --src sqlite+aiosqlite:////app/data/dev.db \
    --dst "postgresql+asyncpg://videomaker:PGPASS@video-maker-postgres-dev:5432/videomaker"
```

预期：每张表打印行数，末尾输出「全部一致。」。若报「搬迁后行数不一致」，**不要继续**，先查清差异。

- [ ] **Step 6: 提交**

```bash
git add backend/app/scripts/pg_migration backend/tests/integration/test_pg_migration.py
git commit -m "feat(db): 加 SQLite→PostgreSQL 数据搬迁脚本

按外键顺序全量搬（content_analyses 最先，events 最后），搬完对齐
SERIAL 序列——不做的话下一次 INSERT 会从 1 开始撞上已搬入的主键，
这是跨库搬迁最经典的坑，专门加了回归测试钉住。

内置逐表行数比对，不一致直接非零退出，避免带着残缺数据切流量。"
```

---

## Task 7: 回归验收

对着 PRD §12.1 的验收条件逐项验证，并确认业务行为没有被改动。

**Files:**
- Create: `backend/tests/integration/test_sse_concurrency.py`

**Interfaces:**
- Consumes: Task 1-6 的全部产出
- Produces: 无（验收任务）

- [ ] **Step 1: 写 SSE 并发测试**

PRD 明确要求「SSE 并发 20 路不耗尽连接池」。历史上 SSE 长连接打爆过 SQLite 连接池（`db.py` 原注释有记录），换 PG + QueuePool 后必须回归。

新建 `backend/tests/integration/test_sse_concurrency.py`：

```python
"""SSE 并发不得耗尽连接池。

历史背景：SSE 流曾经在整个连接生命周期里持有 DB session，导致连接池被
耗尽（见 db.py 的历史注释）。现有代码已改为「快照查询后立即释放 session」，
本测试是迁移到 PostgreSQL + QueuePool 之后对该行为的回归钉子。

关键：这里**必须自建一个带生产连接池配置的引擎**，不能用 tests/conftest.py
的 db_engine —— 后者是 NullPool，池尺寸约束根本不生效，那样测的就不是
我们要验的东西了。用 build_pool_kwargs 拿到与生产完全相同的池配置：
pool_size=5 + max_overflow=10 = 最多 15 条连接，而我们要开 20 路并发。
只有 session 被及时归还，20 路才可能都跑完。
"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import build_pool_kwargs
from app.models.project import Base, Project
from tests.conftest import test_database_url

CONCURRENCY = 20


@pytest.fixture
async def pooled_session_factory():
    """用生产同款 QueuePool 配置建引擎（区别于 db_engine 的 NullPool）。"""
    url = test_database_url()
    pool_kwargs = build_pool_kwargs(url)
    assert "poolclass" not in pool_kwargs, "测试库必须是 PostgreSQL，否则本测试无意义"
    assert pool_kwargs["pool_size"] + pool_kwargs["max_overflow"] < CONCURRENCY, (
        "并发数必须超过池上限，否则测不出 session 是否被及时归还"
    )

    engine = create_async_engine(url, **pool_kwargs)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


@pytest.fixture
async def seeded_project(pooled_session_factory):
    async with pooled_session_factory() as s:
        p = Project(
            id="22222222-2222-2222-2222-222222222222",
            title="并发样例", theme_text="主题", creator_name="wayne",
            status="draft", aspect_ratio="9:16",
        )
        s.add(p)
        await s.commit()
        return p.id


async def test_twenty_concurrent_snapshot_queries(pooled_session_factory, seeded_project):
    async def snapshot() -> str:
        async with pooled_session_factory() as s:
            row = (await s.execute(
                select(Project).where(Project.id == seeded_project)
            )).scalar_one()
            return row.title

    results = await asyncio.wait_for(
        asyncio.gather(*(snapshot() for _ in range(CONCURRENCY))),
        timeout=30,
    )
    assert results == ["并发样例"] * CONCURRENCY
```

- [ ] **Step 2: 运行 SSE 并发测试**

```bash
uv run --project backend pytest tests/integration/test_sse_concurrency.py -v
```

预期：PASS。若卡住超时，说明连接没被释放——查 `db_session_factory` 的使用是否漏了 `async with`。

- [ ] **Step 3: 跑全套后端测试**

```bash
uv run --project backend pytest -q
```

预期：全部通过，且**没有任何** SQLite 相关的警告或错误。

- [ ] **Step 4: 验证空库一键建表**

这是 PRD 的核心验收条件。建一个全新的空库，只跑 `alembic upgrade head`：

```bash
podman exec video-maker-postgres-dev psql -U videomaker -d postgres \
    -c "DROP DATABASE IF EXISTS videomaker_fresh;" \
    -c "CREATE DATABASE videomaker_fresh;"

cd backend && POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
    POSTGRES_USER=videomaker POSTGRES_PASSWORD=devpassword POSTGRES_DB=videomaker_fresh \
    uv run --project . alembic upgrade head

podman exec video-maker-postgres-dev psql -U videomaker -d videomaker_fresh \
    -c "\dt"
```

预期：列出 7 张业务表 + `alembic_version` 表。

- [ ] **Step 5: 确认 _ensure_columns 与生产 create_all 已彻底消失**

```bash
grep -rn "_ensure_columns\|PRAGMA" backend --include=*.py | grep -v __pycache__
grep -rn "create_all" backend/app --include=*.py | grep -v __pycache__
```

预期：三条命令全部无输出。

- [ ] **Step 6: 端到端手工验证**

起全栈，在浏览器里过一遍最基本的读路径（**不要触发任何生成动作**——那会产生真实计费）：

```bash
podman compose -f deploy/docker-compose.dev.yml up -d
curl -s localhost:8002/health
curl -s localhost:8002/api/projects | head -c 400
curl -sI localhost:4000
```

然后打开 `http://localhost:4000`，确认：
- 项目列表能加载出搬迁过来的历史项目
- 点进一个已有项目，分镜列表、素材预览正常
- 浏览器控制台无 500 错误

> **不要点任何「生成」「重新生成」按钮**——本期不碰业务逻辑，也不该产生任何模型调用与账单。

- [ ] **Step 7: 确认业务代码零改动**

```bash
# 基线用第 0 期的实际起点，不要用 master ——
# 本仓库的本地 master 可能落后 HEAD 上百个提交（含大量与本期无关的已合并功能），
# 用它做基线会把别人的改动算到本期头上。
git diff --stat 1a730e0..HEAD -- backend/app/api backend/app/agents backend/app/services backend/worker
```

预期：只有 `backend/app/services/` 下**无**输出，`backend/app/scripts/cos_migration/runner.py` 有改动（那是删建列调用，已在 Task 3 说明）。若 `api/`、`agents/`、`worker/` 出现任何 diff，说明越界了，回退那部分改动。

- [ ] **Step 8: 提交**

```bash
git add backend/tests/integration/test_sse_concurrency.py
git commit -m "test: 加 SSE 并发回归，钉住迁移后连接池不被耗尽

SSE 长连接曾打爆 SQLite 连接池，现有代码已改为快照后立即释放 session。
换成 PostgreSQL + QueuePool(5+10) 后开 20 路并发仍全部拿到结果，
证明释放行为未退化。"
```

---

## 验收清单（对照 PRD §12.1）

全部任务完成后逐条打勾：

- [ ] `alembic upgrade head` 可在空 PostgreSQL 库上一键建出全部表（Task 7 Step 4）
- [ ] `_ensure_columns()` 已删除，代码库中无 `PRAGMA` 残留（Task 7 Step 5）
- [ ] `backend/app/` 下无 `create_all` 调用（Task 7 Step 5）
- [ ] 全部后端测试在 PostgreSQL 上通过（Task 7 Step 3）
- [ ] SSE 并发 20 路不耗尽连接池（Task 7 Step 2）
- [ ] 存量数据搬迁后逐表行数一致（Task 6 Step 5）
- [ ] `api/`、`agents/`、`worker/` 零业务改动（Task 7 Step 7）
- [ ] 三个 Python 服务共用一处数据库配置，无重复 `DATABASE_URL`（Task 5 Step 5）

## 回滚方案

第 0 期不改任何业务逻辑与表结构语义，回滚是干净的：

1. `deploy/docker-compose.dev.yml` 里把 `x-db-env` 锚点引用换回 `DATABASE_URL: sqlite+aiosqlite:////app/data/dev.db`
2. 重启栈：`podman compose -f deploy/docker-compose.dev.yml up -d`

`app-data` 卷里的旧 SQLite 库全程未被写入，`build_pool_kwargs()` 的 SQLite 分支也刻意保留了（见 Task 1 Step 5 的说明），所以切回去立即可用。

唯一不可逆的是 `_ensure_columns()` 的删除——但它的全部职责已由 Alembic initial revision 覆盖，回退到 SQLite 后跑一次 `alembic upgrade head` 同样能建出正确的表结构（Alembic 支持 SQLite）。

## 已知会踩的坑

| 现象 | 原因 | 处理 |
|------|------|------|
| `asyncpg.InvalidPasswordError` | `make secrets` 没跑，或 `secrets.yml` 里没加 `postgres_password` | 补上后 `make secrets` 再重启 |
| backend 启动即退出，报「表结构版本不匹配」 | 没跑 `alembic upgrade head` | 这是 Task 3 的守门在正常工作，跑一次迁移即可 |
| 搬迁后插入报主键冲突 | SERIAL 序列没对齐 | Task 6 的 `copy_all` 已处理；若手工搬过数据，补跑 `setval` |
| 测试报 `UndefinedTableError` | 该测试用了 `db_session_factory` 但没经过 `db_engine` | 检查夹具依赖链 |
| `alembic` 命令报找不到 `app` 模块 | 没在 `backend/` 目录下执行 | `cd backend` 后再跑，`env.py` 里的 `sys.path` 注入依赖这个 cwd |
