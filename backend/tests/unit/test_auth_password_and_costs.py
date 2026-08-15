"""口令哈希与点数单价的纯函数级验证。"""

import pytest

from app.config import settings
from app.services import credits
from app.services.auth import (
    Principal,
    _bearer_token,
    _cookie_value,
    client_ip,
    hash_password,
    verify_password,
)


def test_password_is_never_stored_in_plaintext():
    password = "correct horse battery staple"
    digest = hash_password(password)
    assert password not in digest
    assert digest.startswith("$2")  # bcrypt
    assert verify_password(password, digest)
    assert not verify_password("wrong password", digest)


def test_hash_is_salted_so_same_password_differs():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


def test_long_password_is_not_silently_truncated():
    """bcrypt 只吃 72 字节。预哈希之后，第 73 字节起的差异必须仍然有效——
    否则「超长口令的前 72 字节对上就算通过」，等于把长口令削弱成短口令。"""
    base = "x" * 72
    digest = hash_password(base + "TAIL-A")
    assert verify_password(base + "TAIL-A", digest)
    assert not verify_password(base + "TAIL-B", digest)


def test_corrupt_hash_verifies_false_instead_of_raising():
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_video_priced_per_second_not_per_shot():
    """4|6|8 秒差着 2 倍，按分镜计价会让 8 秒白占 4 秒的便宜。"""
    assert credits.video_cost(4) == 4 * settings.credit_cost_video_per_second
    assert credits.video_cost(8) == 2 * credits.video_cost(4)
    assert credits.video_cost(8) == 120  # 默认单价 15/秒


def test_500_credits_covers_four_eight_second_shots_plus_script():
    """定向投喂基准量 500 点 = 4 个 8 秒分镜 + 剧本 + 分镜表。"""
    total = 4 * credits.video_cost(8) + credits.script_cost() + credits.shotlist_cost()
    assert total == 490
    assert total <= 500


def test_client_ip_prefers_cf_connecting_ip():
    """取 socket 地址会让全站共用一个计数器，第一个注册的人挡住所有人。"""
    headers = {
        b"cf-connecting-ip": b"203.0.113.7",
        b"x-forwarded-for": b"203.0.113.7, 172.16.0.1",
    }
    assert client_ip(headers, "10.42.0.1") == "203.0.113.7"


def test_client_ip_falls_back_to_leftmost_xff_then_socket():
    assert client_ip({b"x-forwarded-for": b"198.51.100.9, 10.0.0.1"}, "10.42.0.1") == "198.51.100.9"
    assert client_ip({}, "10.42.0.1") == "10.42.0.1"
    assert client_ip({}, None) == "unknown"


def test_bearer_and_cookie_parsing():
    assert _bearer_token({b"authorization": b"Bearer abc123"}) == "abc123"
    assert _bearer_token({b"authorization": b"Basic abc123"}) is None
    assert _bearer_token({}) is None
    assert _cookie_value({b"cookie": b"other=1; session=tok3n; x=2"}, "session") == "tok3n"
    assert _cookie_value({b"cookie": b"other=1"}, "session") is None


def test_service_principal_is_not_billable():
    """未绑定账号的机器令牌没有余额可扣，也没有归属可校验。"""
    assert Principal(username="machine", is_machine=True).is_billable is False
    assert Principal(username="alice", user_id="u1").is_billable is True


@pytest.mark.parametrize("amount", [0, -5])
async def test_reserve_is_a_noop_for_zero_or_negative_cost(amount):
    assert await credits.reserve(
        Principal(username="alice", user_id="u1"), amount,
        ref_type="t", ref_id="r",
    ) is None


async def test_reserve_is_a_noop_without_identity():
    """AUTH_ENFORCED=false 下的匿名调用不扣点数——行为与鉴权上线前一致。"""
    assert await credits.reserve(None, 100, ref_type="t", ref_id="r") is None
