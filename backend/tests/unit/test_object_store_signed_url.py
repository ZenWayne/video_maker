"""object_store.signed_url 的 TTL 钳制——纯本地逻辑，不需要真实 COS 凭证。

brief 要求签名有效期取 min(配置 TTL, 凭证剩余有效期)，但所有现有集成测试
都在 static 鉴权模式下跑（`credentials_remaining_sec()` 恒返回 None），
从未真正触发过这条 min() 分支——写错方向或写错变量都测不出来，只有到
cvm_role 生产部署才会暴露。

参考 test_cos_client_refresh.py 的做法：直接操作 cos_client 的模块级状态
（`_cached_cred` / `_client` / `_cred_expires_at`）模拟 cvm_role 模式下
凭证即将过期，断言 signed_url() 生成的 URL 里签入的有效期真的被钳到了
剩余凭证秒数，而不是配置的 cos_signed_url_ttl_sec。get_presigned_url 是
纯本地 HMAC 计算，不发任何网络请求，因此全程不需要真实凭证/网络。
"""
import time
from urllib.parse import parse_qs, urlparse

from app.config import settings
from app.services import cos_client, object_store


def _extract_signed_ttl(url: str) -> int:
    """从预签名 URL 里反推 Expired 参数：q-sign-time = bg;ed，bg = now-60。"""
    qs = parse_qs(urlparse(url).query)
    bg, ed = qs["q-sign-time"][0].split(";")
    return int(ed) - int(bg) - 60


def _fake_static_client(monkeypatch):
    """让 get_client()/bucket() 在不发网络请求的前提下可用：伪造凭证与桶配置。

    get_presigned_url 只做本地字符串拼接 + HMAC，伪造的 SecretId/SecretKey/
    Bucket 不会被校验，也不会触发任何请求。
    """
    monkeypatch.setattr(settings, "cos_bucket", "fake-bucket-1250000000")
    monkeypatch.setattr(settings, "cos_region", "ap-guangzhou")
    monkeypatch.setattr(settings, "cos_scheme", "https")
    monkeypatch.setattr(settings, "cos_domain", None)
    monkeypatch.setattr(cos_client, "_client", None)
    monkeypatch.setattr(
        cos_client, "_cached_cred",
        {"secret_id": "fake-id", "secret_key": "fake-key", "token": None},
    )


def test_signed_url_clamps_ttl_to_remaining_credentials(monkeypatch):
    """cvm_role 模式下凭证 300 秒后过期 -> 签名有效期必须被钳到 ~300，而非配置的 7200。"""
    _fake_static_client(monkeypatch)
    monkeypatch.setattr(settings, "cos_signed_url_ttl_sec", 7200)
    monkeypatch.setattr(cos_client, "_cred_expires_at", time.time() + 300)

    url = object_store.signed_url("test/x.txt")
    ttl = _extract_signed_ttl(url)

    # 允许生成耗时带来的 ±2 秒抖动；核心断言是钳到剩余凭证附近，而非 7200
    assert 295 <= ttl <= 300, f"TTL 未被钳到凭证剩余有效期，实际签入 {ttl} 秒"


def test_signed_url_uses_configured_ttl_when_credentials_never_expire(monkeypatch):
    """static 鉴权模式（凭证永不过期）下不该被钳——直接使用配置 TTL。"""
    _fake_static_client(monkeypatch)
    monkeypatch.setattr(settings, "cos_signed_url_ttl_sec", 7200)
    monkeypatch.setattr(cos_client, "_cred_expires_at", None)  # static 模式：永不过期

    url = object_store.signed_url("test/x.txt")
    ttl = _extract_signed_ttl(url)

    assert ttl == 7200


def test_signed_url_respects_explicit_expires_sec_under_clamp(monkeypatch):
    """显式传入的 expires_sec 仍要服从 min(expires_sec, 凭证剩余)。"""
    _fake_static_client(monkeypatch)
    monkeypatch.setattr(cos_client, "_cred_expires_at", time.time() + 300)

    url = object_store.signed_url("test/x.txt", expires_sec=60)
    ttl = _extract_signed_ttl(url)

    assert ttl == 60  # 60 < 300 剩余，min() 应取 60，不应被放大
