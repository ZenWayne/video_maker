"""COS 配置字段的默认值与类型约束。"""
from app.config import Settings


def test_cos_defaults_are_safe():
    """未配置时默认走 static 模式、https、2 小时 TTL。"""
    s = Settings(_env_file=None)
    assert s.cos_auth_mode == "static"
    assert s.cos_scheme == "https"
    assert s.cos_signed_url_ttl_sec == 7200
    assert s.cos_domain is None
    assert s.cos_secret_id is None
    assert s.cos_secret_key is None


def test_cos_auth_mode_accepts_cvm_role():
    s = Settings(_env_file=None, cos_auth_mode="cvm_role", cos_cvm_role="my-role")
    assert s.cos_auth_mode == "cvm_role"
    assert s.cos_cvm_role == "my-role"


def test_cos_bucket_keeps_appid_suffix():
    """COS 的 bucket 名必须含 AppId，配置层不得擅自截断。"""
    s = Settings(_env_file=None, cos_region="ap-guangzhou",
                 cos_bucket="video-maker-dev-1250000000")
    assert s.cos_region == "ap-guangzhou"
    assert s.cos_bucket == "video-maker-dev-1250000000"


def test_no_legacy_oss_fields_remain():
    """OSS 字段必须彻底移除——两套并存会让 cos_client 读到过期配置。"""
    s = Settings(_env_file=None)
    leftovers = [f for f in type(s).model_fields if f.startswith("oss_")]
    assert leftovers == [], f"残留 OSS 配置字段: {leftovers}"
