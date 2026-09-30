# Deferred spark-node test plan — merge 7492f2f (upstream main 674155d, 2026-10-01)

**状态**: 本地（macOS）已完成 merge + `bash -n` + AST parse + pytest 基线 diff
（FAILED 名单与 pristine upstream/main **完全一致**，双向 comm 为空，45F 均为 macOS
环境失败：flock 缺失、Xcode license、bash 3.2 `set -u` 空数组、torch session 污染；
非合并回归）。远端 4 台 spark 节点全部运行生产服务，以下步骤**未执行**，待维护
窗口由运维按序手动启动。

## 背景：本次合入（upstream main e5761b2 → 674155d，15 commits）

| commit / 内容 | 性能影响 |
|---|---|
| e5dc885: `start-tp4.sh` 默认 `EXL3_FAT_GROUPED=1`（耦合 `EXL3_TEMP_ROWS_FUSED=32`；注释: 2026-09-27 4 Sparks 实测 8K/32K/100K prefill 提速，decode 持平） | **TP4 默认形态变更**（prefill 提速；与 start.sh/start-tp3.sh 既有默认对齐）；生产 .env.tp4 若显式设 0 则不受影响 |
| 9dbd687 `fix(scheduler)`: fair-prefill 候选优先级（overlay/patch_scheduler_decode_floor.py +33；tests/test_scheduler_prefill_priority.py） | runtime scheduler 行为修复（公平性），perf 影响待测 |
| d223820 `fix(coop)`: cooperative_moe.so 校验从 ELF SHA256 改为 provenance（prepare_profile.py 部署前校验）+ ABI/layout/occupancy 运行时校验保留 | 移除 hash 失配引发的启动失败面；无 perf 影响 |
| f9848fb: `start-tp4.sh` 向各 rank 转发 `EXL3_FAT_GROUPED`（e5dc885 的前置） | e5dc885 的机制部分 |
| 891f6c8 `test`: tests/test_image_layer_budget.py — CPU 上检测镜像超 overlay2 可运行层深 | 测试 only |
| e74e517/e2eda6/40b993a/739d87e: README 缩短 + docs/REFERENCE.md 全量参考 + opt-in 标注 + perf 图表 | 文档 only |

fork delta 存活核验（合并后）: raw host-dir 权重模式（MODEL_HOST_DIR/DFLASH_HOST_DIR）、
4× `--ulimit nofile=524288:524288`、`GLM53_DENSE_FP8` fork-only 转发、
`tests/test_tp4_raw_host_dirs.py`、`.env.tp4.example` raw-dir 段、
`tests/test_exl3_overlay.py` compact-selection 断言。adaptive-k 无 fork 残留重复
（上次 8750460 去重后合并干净引入）。合并为 clean ort，无冲突。

## 测试前置

- [ ] spark-eb1a（head）+ 3 workers 各自 `cd /home/colin/nvmodels/GLM-5.3-Flash-EXL3-2x-DGX-Sparks && git fetch origin && git checkout 7492f2f`（或 push 后的 main tip）
- [ ] `git log --oneline -1`；`bash -n start.sh start-tp3.sh start-tp4.sh`
- [ ] **不要**在生产容器运行期间做任何 GPU 构建/重启

## T1 — stock 路径无回归（最高优先）

1. **确认现网 .env.tp4 的 `EXL3_FAT_GROUPED` 设定**: 未设 → 新默认 1 生效（boot 日志
   `fat_grouped=1 temp_rows=32`）；显式 `=0` → 维持 E2 tier 不变。如有疑虑，维护窗口
   先在 .env.tp4 显式设 `EXL3_FAT_GROUPED=0` 锁定旧形态再重启
2. 不设新 knob 启动 TP4: `/v1/chat/completions` 冒烟 + warmup canary rc==0；对比
   合并前 decode receipt 指标无漂移
3. scheduler 公平prefill 修复观察: 混合长短并发下无异常 prefill 饥饿/队列积压告警
4. raw host-dir 模式复验: `MODEL_HOST_DIR`/`DFLASH_HOST_DIR` 启动 → preflight per-rank `test -d` 通过、skip download/rsync 日志在、`:ro` 挂载生效

## T2 — cooperative_moe provenance 校验（d223820）

仅当集群实际启用 cooperative MoE 时相关；否则跳过。

1. 启动日志无 `unvalidated cooperative_moe.so digest` 拒绝（旧 hash 校验已移除）
2. ABI/layout/occupancy 校验照常通过（这几条 fail-closed 保留）

## T3 — opt-in 功能（仅维护窗口需要时）

1. TP4 adaptive-k / `GLM53_DEFAULT_REASONING_EFFORT`（沿上游 #235/#303 语义，见
   deferred-spark-tests-2026-09-30b.md T3，本次合入未改）
2. GLM53_DENSE_FP8（fork-only）默认 off 不变

## T4 — 回退

任一步失败: `git checkout 8750460`（= 本次 merge 前形态）。e5dc885 未参与时旧镜像
不受影响；新镜像可 `docker rmi` 回旧 tag。`.env.tp4` 若为回退显式设了
`EXL3_FAT_GROUPED=0`，恢复默认时记得删除该行。
