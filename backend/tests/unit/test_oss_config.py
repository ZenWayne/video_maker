"""OSS 配置字段的默认值与类型约束。"""
from app.config import Settings


def test_oss_defaults_are_safe():
    """未配置时默认走 static 模式、公网 endpoint、2 小时 TTL。"""
    s = Settings(_env_file=None)
    assert s.oss_auth_mode == "static"
    assert s.oss_use_internal_endpoint is False
    assert s.oss_signed_url_ttl_sec == 7200
    assert s.oss_endpoint is None
    assert s.oss_access_key_id is None
    assert s.oss_access_key_secret is None


def test_oss_auth_mode_accepts_ecs_ram_role():
    s = Settings(_env_file=None, oss_auth_mode="ecs_ram_role", oss_ecs_ram_role="my-role")
    assert s.oss_auth_mode == "ecs_ram_role"
    assert s.oss_ecs_ram_role == "my-role"


def test_oss_bucket_and_region_are_plain_strings():
    s = Settings(_env_file=None, oss_region="cn-hangzhou", oss_bucket="video-maker-dev")
    assert s.oss_region == "cn-hangzhou"
    assert s.oss_bucket == "video-maker-dev"
