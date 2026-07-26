"""VC 前视频备份 —— 纯 service 模块，禁止依赖 app.main / FastAPI。

必须待在这里（而不是 app/api/pipeline.py）：调用方之一是 worker/tasks.py 的
_do_voice_convert_one，跑在 vc-worker 进程里（deploy/docker-compose.dev.yml
的 arq worker.vc_arq_worker.VcWorkerSettings）。该进程只 import worker.tasks，
从不 import app.main。若这个函数改从 app.api.pipeline 导入，而 pipeline.py
顶层又有 `from app.main import get_redis`，vc-worker 处理第一个
run_voice_convert 任务时就会触发 app.main 的路由 import 链——若那条链此刻
还没被别的入口跑过（vc-worker 独立进程里从来没有），会撞上"正在加载中的
半成品模块" ImportError（app.api.pipeline 自己 import 到一半时又被 app.main
反向 import）。这不是本地能复现的假设，是真实对着 vc-worker 的启动命令跑出来
的：ImportError: cannot import name '_require_user' from partially initialized
module 'app.api.pipeline'。且这个 import 语句在 _do_voice_convert_one 自己的
try/except 之外、run_voice_convert(_batch) 外层也没有兜底，异常会直接冒到 arq
的失败路径——而端点早已把 shot.vc_status 置成 "converting"，此后无人再改，
表现为"每次语音转换都永远卡在转换中，UI 上看不到任何错误"。
"""

from sqlalchemy import select

from app.models.project import Shot
from app.services import object_store
from app.services.storage import shot_key


async def ensure_pre_vc_backup(session_factory, project_id: str, shot_id: int) -> str:
    """确保 VC 前的原视频已备份。返回备份 key。幂等。

    用 COS 服务端 copy——不产生本地流量，比本地 shutil.copy 还快。已备份
    （pre_vc_video_key 已设置）时直接返回原备份 key，绝不重复拷贝。
    """
    async with session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        if shot.pre_vc_video_key:
            return shot.pre_vc_video_key
        src = shot.video_path

    backup_key = shot_key(project_id, shot_id, "output_pre_vc.mp4")
    await object_store.copy(src, backup_key)

    async with session_factory() as s:
        shot = (await s.execute(
            select(Shot).where(Shot.project_id == project_id, Shot.shot_id == shot_id)
        )).scalar_one()
        shot.pre_vc_video_key = backup_key
        await s.commit()
    return backup_key
