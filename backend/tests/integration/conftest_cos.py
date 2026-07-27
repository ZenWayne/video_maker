"""COS 集成测试夹具：唯一前缀隔离 + teardown 清理。

按项目规则，COS 不 mock —— 这些测试打真实 dev bucket。
未配置时自动 skip，便于无凭证环境跑其余测试。
"""
import uuid

import pytest

from app.config import settings


def _cos_configured() -> bool:
    if not settings.cos_bucket or not settings.cos_region:
        return False
    if settings.cos_auth_mode == "static":
        return bool(settings.cos_secret_id and settings.cos_secret_key)
    return True


requires_cos = pytest.mark.skipif(
    not _cos_configured(),
    reason="需要 COS 凭证与 dev bucket（设置 cos_region/cos_bucket/cos_secret_*）",
)


@pytest.fixture
async def cos_prefix():
    """本次测试专属 key 前缀，退出时递归删除。

    需要缓存凭证才能拿到 client（object_store 不负责 warm——那是应用
    lifespan/worker 启动时的职责，见 cos_client.warm_credentials 的文档），
    这里显式预热一次，反映真实调用序并让本 fixture 的 teardown 也能工作。
    """
    from app.services import cos_client, object_store

    await cos_client.warm_credentials()
    prefix = f"test/{uuid.uuid4().hex}/"
    yield prefix
    await object_store.delete_prefix(prefix)
