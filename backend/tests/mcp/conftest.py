"""Fixtures for MCP server tests: real backend ASGI app over PostgreSQL."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport

from app.models.project import Project, Shot

# conftest_cos.py (under tests/integration/) defines the cos_prefix fixture used
# by COS-backed MCP tests. Re-export it here for the same reason
# tests/integration/conftest.py does — pytest doesn't auto-discover a
# non-conftest.py module, and a non-top-level conftest can't use pytest_plugins.
from tests.integration.conftest_cos import cos_prefix  # noqa: F401
from tests.integration.conftest import install_fake_cos_credentials  # noqa: F401

USER = "mcp-agent"


@pytest.fixture
async def http_client(db_engine, db_session_factory, monkeypatch):
    from app.main import app, get_redis
    from app.db import get_session
    import app.db as db_module
    import app.api.stream as stream_module
    import app.api.pipeline as pipeline_module
    import app.api.voice as voice_module

    monkeypatch.setattr(db_module, "AsyncSession", db_session_factory)
    monkeypatch.setattr(stream_module, "session_factory", db_session_factory)

    arq = MagicMock()
    arq.enqueue_job = AsyncMock(return_value=None)

    async def _fake_get_arq(_redis):
        return arq
    monkeypatch.setattr(pipeline_module, "_get_arq_redis", _fake_get_arq)
    monkeypatch.setattr(voice_module, "_get_arq_redis", _fake_get_arq)

    async def override_session():
        async with db_session_factory() as s:
            yield s

    async def override_redis():
        return AsyncMock()

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_redis] = override_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def backend(http_client):
    from mcp_server.client import BackendClient
    return BackendClient(base_url="http://test", client=http_client)


# ── DB seed helpers ──────────────────────────────────────────────────────────

async def seed_project(sf, status="script_review", scene_overview="overview", shots=3):
    async with sf() as s:
        p = Project(title="P", theme_text="theme", creator_name=USER,
                    status=status, scene_overview=scene_overview)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        pid = p.id
        for i in range(1, shots + 1):
            s.add(Shot(project_id=pid, shot_id=i, text=f"line {i}",
                       shot_type="Medium Shot", visual_description=f"v{i}",
                       shot_duration=6, status="pending", align_with_previous=(i > 1)))
        await s.commit()
        return pid


async def seed_reference_image(sf, project_id, kind="character"):
    from app.models.project import ReferenceImage
    async with sf() as s:
        s.add(ReferenceImage(project_id=project_id, kind=kind,
                             filename="ref.png", storage_path="refs/ref.png",
                             order_index=0))
        await s.commit()
