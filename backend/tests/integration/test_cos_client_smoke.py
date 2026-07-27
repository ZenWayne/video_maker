"""cos_client 冒烟：单例、凭证预热、干净关闭。"""
from tests.integration.conftest_cos import requires_cos

from app.services import cos_client


@requires_cos
async def test_client_is_singleton():
    """SDK 要求一个 region 只建一个实例并复用，否则占用过多连接和线程。

    生产环境下 lifespan 总是先 warm_credentials() 再触碰 get_client()——
    get_client() 依赖缓存凭证（见 cos_client.get_cached_credentials）,
    这里显式预热一次以保证测试独立于执行顺序、并反映真实调用序。
    """
    await cos_client.warm_credentials()
    a = cos_client.get_client()
    b = cos_client.get_client()
    assert a is b


@requires_cos
async def test_warm_credentials_populates_cache():
    await cos_client.warm_credentials()
    cred = cos_client.get_cached_credentials()
    assert cred["secret_id"]
    assert cred["secret_key"]


@requires_cos
async def test_close_client_is_idempotent():
    cos_client.get_client()
    await cos_client.close_client()
    await cos_client.close_client()  # 第二次不应抛异常
