"""cos_client 后台刷新的休眠计算：falsy-zero 回归测试。

`credentials_remaining_sec()` 在凭证已完全过期时正确返回 0，但 0 在
Python 里是 falsy。`_refresh_loop` 曾经写成 `remaining or 3600`，把这个
合法的 0 误判成"还没算出来"，从而按默认 TTL 睡 1800 秒——把一次瞬时刷新
失败放大成最长 30 分钟的凭证失效窗口。这里直接测试纯函数
`_refresh_sleep_seconds`，并通过操作模块级 `_cred_expires_at` 验证
`credentials_remaining_sec()` 在凭证过期时返回 0、且该 0 被正确传导为
60 秒的下限休眠，而不是被短路成 3600 的一半。

不需要 COS 凭证——这是纯本地逻辑，不发任何网络请求。
"""
import time

from app.services import cos_client


def test_refresh_sleep_seconds_treats_zero_as_expired_now():
    """0 必须被当作「已过期，立即重试」，不能被 `or` 短路成默认 TTL。"""
    assert cos_client._refresh_sleep_seconds(0) == 60


def test_refresh_sleep_seconds_defaults_when_remaining_is_none():
    """None（static 模式 / 尚未预热）按默认 TTL 处理：睡 3600 // 2 = 1800。"""
    assert cos_client._refresh_sleep_seconds(None) == 1800


def test_refresh_sleep_seconds_halves_positive_remaining():
    assert cos_client._refresh_sleep_seconds(240) == 120
    # 剩余秒数很小时仍保持 60 秒下限，不会退化成忙等
    assert cos_client._refresh_sleep_seconds(10) == 60


def test_expired_credentials_remaining_sec_is_zero_and_drives_immediate_retry():
    """端到端：模块级过期时间戳已过去 -> remaining=0 -> 休眠 60 秒而非 1800 秒。"""
    original_expires_at = cos_client._cred_expires_at
    try:
        cos_client._cred_expires_at = time.time() - 1  # 已经过期
        remaining = cos_client.credentials_remaining_sec()
        assert remaining == 0

        sleep_for = cos_client._refresh_sleep_seconds(remaining)
        assert sleep_for == 60, (
            "凭证已过期时必须立即重试（60 秒下限），而不是被 `0 or 3600` "
            "误判成默认 TTL 睡 1800 秒"
        )
    finally:
        cos_client._cred_expires_at = original_expires_at
