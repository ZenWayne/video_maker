"""COS 集成测试的公共 shot 视频 seeding helper。

不是 conftest.py（pytest 不会自动发现），只是被显式 import 的普通模块——
和 conftest_cos.py 是同一约定（见那边的 cos_prefix 注释）。

真正的实现已经在 tests/integration/conftest.py 的 seed_shot_with_source 里
（Tasks 5/6/7/8/9 起复用至今：真实 ffmpeg 合成视频 + 真实 object_store.put +
更新 shot.video_path/source_fps/source_frames）。这里只是给 Task 12 的
test_cos_media_url.py 用一个更具描述性的名字重新导出，避免再实现一遍。
"""
from tests.integration.conftest import seed_shot_with_source as seed_shot_source_to_oss

__all__ = ["seed_shot_source_to_oss"]
