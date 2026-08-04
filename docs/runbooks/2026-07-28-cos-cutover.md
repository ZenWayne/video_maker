# COS 存量迁移与生产切换手册

## 执行前提

- 本仓 Spec A（存储层 COS 化）已部署
- **bucket 多版本控制已开启**——实测 `video-maker-dev-1414782845` 当前
  `VersioningConfiguration: None`（未开启）。这是切换前必须先在腾讯云控制台
  手动完成的第一步，本手册的任何命令都不会替你做这件事。
- 已确认 bucket 与 CVM 同地域（跨地域会让每次 ffmpeg 取素材按外网下行计费）

## 七步

所有步骤共用同一个**绝对、固定**的 `--report-dir`：`/app/data/migration_report`。
它落在 `app-data` 这个共享命名卷里（**特意不放进 `/app/storage`**——那是
`--storage-root`，`--scan`/`--upload` 会递归遍历它当作媒体文件；报告写在
里面会被当成本地对象误传/误报），因此不管是在运行中的容器里执行、还是
第 5/7 步停服后用一次性 `podman run` 执行，看到的都是同一份磁盘、同一个目录
——不会出现「`--scan` 和 `--verify` 用了不同 CWD、导致 `--report-dir` 默认值
各自解析成不同目录」的问题。**下面每条命令都显式带上这个 `--report-dir`，不要
省略、不要改成相对路径。**

1. **备份 DB**：`cp` sqlite 文件到带时间戳的副本；确认 bucket 版本控制已开启
2. **在线 `--upload`**（不停服，最耗时的一步在此消化；容器仍在跑，用
   `podman compose exec`）：
   ```bash
   podman compose -f deploy/docker-compose.dev.yml exec backend \
       uv run --project . python -m app.scripts.migrate_to_cos \
       --upload --storage-root /app/storage --report-dir /app/data/migration_report
   echo "exit=$?"
   ```
   **退出码必须是 0 才能继续。** 非 0 表示 `failed` 列表非空——即有对象上传
   失败，脚本会把失败详情打到 stderr 并指向报告文件；照着报告用
   `--retry-failed` 重试，直到这一步干净地退出 0，再往下走。
3. **停止服务**：`podman compose -f deploy/docker-compose.dev.yml down`
4. **再次 `--upload` 补增量**（停服前最后写入的文件；容器已经停了，改用
   一次性 `podman run`，挂载方式与 CLAUDE.md「Always Run Python via Podman
   Compose」一节一致，另外显式挂上共享的 `app-data` / `app-storage` 命名卷）：
   > **`--upload` 需要 COS 凭证**（它要 `warm_credentials` + `put`），所以和
   > 第 7 步的 `--verify` 一样，必须挂上 `deploy/secrets` 并加载
   > `deploy/config.env`（后者提供 `cos_region` / `cos_bucket`）。只有
   > `--scan` 和 `--backfill` 不碰 COS，可以省掉这两样。

   ```bash
   podman run --rm --network host \
       -v deploy_app-data:/app/data \
       -v deploy_app-storage:/app/storage \
       -v $(pwd)/backend:/app:z -w /app \
       -v $(pwd)/deploy/secrets:/run/secrets:ro,z \
       --env-file deploy/config.env \
       -e DATABASE_URL=sqlite+aiosqlite:////app/data/dev.db \
       ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
       sh -c 'export COS_SECRET_ID=$(cat /run/secrets/cos_secret_id) &&
              export COS_SECRET_KEY=$(cat /run/secrets/cos_secret_key) &&
              uv run --project . python -m app.scripts.migrate_to_cos \
              --upload --storage-root /app/storage --report-dir /app/data/migration_report'
   echo "exit=$?"
   ```
   同样：**退出码必须是 0 才能继续**，否则先排查 `failed` 列表再往下走。
5. **先 `--scan` 后 `--backfill`**——顺序不能反，两条命令都用同一个
   `/app/data/migration_report`：
   ```bash
   podman run --rm --network host \
       -v deploy_app-data:/app/data \
       -v deploy_app-storage:/app/storage \
       -v $(pwd)/backend:/app:z -w /app \
       -e DATABASE_URL=sqlite+aiosqlite:////app/data/dev.db \
       ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
       uv run --project . python -m app.scripts.migrate_to_cos \
       --scan --storage-root /app/storage --report-dir /app/data/migration_report

   podman run --rm --network host \
       -v deploy_app-data:/app/data \
       -v deploy_app-storage:/app/storage \
       -v $(pwd)/backend:/app:z -w /app \
       -e DATABASE_URL=sqlite+aiosqlite:////app/data/dev.db \
       ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
       uv run --project . python -m app.scripts.migrate_to_cos \
       --backfill --storage-root /app/storage --report-dir /app/data/migration_report
   echo "exit=$?"
   ```
   **`--backfill` 的退出码必须是 0 才能继续部署（第 6 步）。** 非 0 表示
   `unrecognized` 非空——有值没能识别、没被回填，脚本会把条数打到 stderr；
   在部署新代码之前必须先人工核实这些值，不要带着未回填的字段往下走。

   > **顺序陷阱一（Spec B §9.2，建列）**：两个 key 列由 `app/db.py` 的幂等
   > `ALTER TABLE` 创建，但 `backfill()`（`runner.py`）**不会**调用
   > `init_db()`——`init_db()` 写死操作 `app.db` 模块级、指向真实共享
   > `dev.db` 的 engine，测试里用它会绕过传入的 `session_factory` 直接打
   > 共享库，这正是本项目明令禁止的事。`backfill()` 改为直接对调用方传入
   > 的 `conn` 调用幂等的 `_ensure_columns(conn)` 来建列，因此永远作用在
   > 正确的引擎上；该例程幂等，重复执行无害，但**建列这一步确实是
   > `--backfill` 内部自动做的，不需要、也不应该额外手动跑一次 `init_db()`
   > 或替换性的建列脚本**。
   >
   > **顺序陷阱二（第 7 步的悬空基线）**：`--verify` 从 `--report-dir` 下的
   > `scan.json` 读取悬空基线；若该文件不存在，`--verify` 现在会在 stderr
   > 打出显眼警告（报告仍会跑完），但没有基线的话，届时全部约 212 条已知
   > 悬空引用都会被计入 `missing_unexpected` 并让退出码变成 1，一次完全
   > 健康的迁移会看起来像是彻底失败。**所以 `--scan` 必须先于 `--verify`
   > 运行，并且两者要用上面这同一个 `/app/data/migration_report`。**
6. **部署新代码**
7. **启动 + `--verify`**：退出码 0 即全绿。启动步骤本身会拉起容器，但
   `--verify` 需要 COS 凭证，用一次性 `podman run` 时要把 `cos_secret_id` /
   `cos_secret_key` 这两个 secret 文件读进环境变量（做法与
   `docker-compose.dev.yml` 里 backend 服务的 `command:` 一致）：
   ```bash
   podman run --rm --network host \
       -v deploy_app-data:/app/data \
       -v deploy_app-storage:/app/storage \
       -v $(pwd)/backend:/app:z -w /app \
       -v $(pwd)/deploy/secrets:/run/secrets:ro,z \
       --env-file deploy/config.env \
       -e DATABASE_URL=sqlite+aiosqlite:////app/data/dev.db \
       ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
       sh -c 'export COS_SECRET_ID=$(cat /run/secrets/cos_secret_id) &&
              export COS_SECRET_KEY=$(cat /run/secrets/cos_secret_key) &&
              uv run --project . python -m app.scripts.migrate_to_cos \
              --verify --storage-root /app/storage --report-dir /app/data/migration_report'
   echo "exit=$?"
   ```

## 验收

- `--verify` 退出码 0（`missing_unexpected` 为空）
- 人工抽查若干历史项目：视频可播放、尾帧可显示、成片可下载
- 抽查一个做过 CC 的历史分镜，确认还原正常（验证 `pristine_last_frame_key` /
  `pre_cc_last_frame_key` 填对了）

## 回滚

恢复 DB 备份 + 回滚代码。**COS 上的对象不删除**（保留对下次重试有用，且只占存储费）。

## 迁移后

- 本地 `storage/` 目录**先保留一到两周**作为回滚保险。**在确认两个 key 列
  （`pristine_last_frame_key` / `pre_cc_last_frame_key`）已正确填充之前，绝不
  删除本地 `storage/` 目录**——这两列只能从本地目录派生，本地目录一旦清空，
  这些信息就永远补不回来。派生列是一次性机会：本次针对生产 DB 副本的完整
  dry run 中，因为只有 8 个项目目录还留在本地磁盘，最终也只派生出
  `pristine_last_frame_key` 23 个、`pre_cc_last_frame_key` 1 个；其余全部
  已不可恢复。
- 一周内配置生命周期规则（不做自动 GC 时，这是控制存储成本的唯一手段）
- 设置流量告警（免费额度不含流量，流量是本项目主要成本项）

## 已知的预期缺失

针对生产 DB 副本的一次完整 dry run 实测：共 349 条媒体引用、跨 14 个列，
`--backfill` 转换了 332 个值（312 个标量 + JSON 数组项），3 个本已是 key
形式被跳过，0 个无法识别；第二次运行 `changed == 0`，满足幂等验收标准。

这 349 条引用里有 **212 条**的本地文件在迁移**之前**就已不存在（55 个项目
目录整体消失，含 19 个 `shot_review`、2 个 `exported`）。这是既有破损，迁移
无法修复。`--scan` 会把它们固化成悬空基线，`--verify` 将其计入
`missing_expected` 而不判失败。**基线之外的任何缺失都必须当作真故障处理。**

## 孤儿对象报告的噪音

```bash
uv run --project backend python -m app.scripts.cos_orphan_report \
    --older-than-days 7
```

dev bucket 是共享的：另一个未合并的 content-analysis 分支的 worktree 会向同一
bucket 写入 `analyses/<uuid>/sample_N/source.mp4`。一次实测报告出 27 个孤儿，
其中 15 个来自那个分支，并非本项目遗留。**首次运行的报告不是干净信号**，不要
把它当作本项目的对象泄漏证据；核对孤儿 key 的前缀 / 归属后再判断是否需要处理。
本工具永远是 dry-run，不会删除任何对象。
