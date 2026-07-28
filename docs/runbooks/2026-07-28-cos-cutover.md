# COS 存量迁移与生产切换手册

## 执行前提

- 本仓 Spec A（存储层 COS 化）已部署
- **bucket 多版本控制已开启**——实测 `video-maker-dev-1414782845` 当前
  `VersioningConfiguration: None`（未开启）。这是切换前必须先在腾讯云控制台
  手动完成的第一步，本手册的任何命令都不会替你做这件事。
- 已确认 bucket 与 CVM 同地域（跨地域会让每次 ffmpeg 取素材按外网下行计费）

## 七步

1. **备份 DB**：`cp` sqlite 文件到带时间戳的副本；确认 bucket 版本控制已开启
2. **在线 `--upload`**（不停服，最耗时的一步在此消化）
   ```bash
   uv run --project backend python -m app.scripts.migrate_to_cos \
       --upload --storage-root /app/storage
   ```
3. **停止服务**：`podman compose -f deploy/docker-compose.dev.yml down`
4. **再次 `--upload` 补增量**（停服前最后写入的文件）
5. **先 `--scan` 后 `--backfill`**——顺序不能反：
   ```bash
   uv run --project backend python -m app.scripts.migrate_to_cos \
       --scan --storage-root /app/storage --report-dir migration_report
   uv run --project backend python -m app.scripts.migrate_to_cos \
       --backfill --storage-root /app/storage --report-dir migration_report
   ```
   > **顺序陷阱一（Spec B §9.2，建列）**：两个 key 列由 `app/db.py` 的幂等
   > `ALTER TABLE` 在应用启动时创建，而本步骤早于第 6 步部署后的首次启动。
   > `--backfill` 已在内部先调 `init_db()` 建列，**不要**跳过或替换该步骤。
   >
   > **顺序陷阱二（第 7 步的悬空基线）**：`--verify` 从 `--report-dir` 下的
   > `scan.json` 读取悬空基线；若该文件不存在，`--verify` 会静默地在无基线的
   > 情况下运行，届时全部约 212 条已知悬空引用都会被计入 `missing_unexpected`，
   > 一次完全健康的迁移会看起来像是彻底失败，且没有任何解释。**所以 `--scan`
   > 必须先于 `--verify` 运行，并且两者要用同一个 `--report-dir`。**
6. **部署新代码**
7. **启动 + `--verify`**：退出码 0 即全绿
   ```bash
   uv run --project backend python -m app.scripts.migrate_to_cos \
       --verify --storage-root /app/storage --report-dir migration_report
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
