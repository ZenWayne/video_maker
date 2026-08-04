"""守卫：集成测试不许在 dev bucket 的 projects/ 前缀下留孤儿对象。

历史教训：seed_shot_with_source 直接发布到真实 projects/<uuid>/ 且无
teardown，攒出 2893 个孤儿对象（1581 个 DB 里不存在的 project id），
把孤儿巡检报告淹没成噪音。
"""
import pytest

from tests.integration.conftest import _add_shot, _make_project, seed_shot_with_source
from tests.integration.conftest_cos import requires_cos


@requires_cos
async def test_seeded_shot_objects_are_cleaned_up(db_session_factory, cos_prefix):
    from app.services import object_store

    pid = await _make_project(db_session_factory, status="shot_review")
    await _add_shot(db_session_factory, pid, 1)
    key = await seed_shot_with_source(db_session_factory, pid, 1, frames=12)
    assert await object_store.exists(key)

    # 模拟 teardown：本用例结束后 autouse fixture 会删掉 projects/<pid>/，
    # 这里显式调一次同样的清理并断言它确实生效。
    from tests.integration.conftest import cleanup_test_project_prefixes
    await cleanup_test_project_prefixes()
    assert not await object_store.exists(key)
