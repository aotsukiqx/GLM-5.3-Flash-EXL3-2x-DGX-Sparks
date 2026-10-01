# Deferred spark-node test plan — merge 507b216 (upstream main 6278ecb, 2026-10-01)

**状态**: 本地（macOS）已完成 merge + `bash -n` + AST parse ×9 + pytest 全量基线 diff
（FAILED 名单 = 上次合并基线 + 1 个新 macOS 环境失败，其余 45 个全部为已知环境失败：
flock 缺失、Xcode license、bash 3.2 `set -u` 空数组、torch session 污染）。远端 4 台
spark 节点全部运行生产服务，以下步骤**未执行**，待维护窗口由运维按序手动启动。

## 背景：本次合入（upstream main 674155d → 6278ecb，9 commits，PR #309）

decode-floor **v7** 整合（#283 系列，整合 #246/#221/#180）：

| commit / 内容 | 性能影响 |
|---|---|
| b94fdfd: scheduler 集成 #246/#221/#180 为 decode-floor v7（overlay/patch_scheduler_decode_floor.py +636 行重写） | runtime scheduler 行为变更（v6→v7），perf 影响待测 |
| 7340644: unpatch 后残留 decode-floor marker 拒绝；canary 拒绝任何 URL userinfo | 健壮性/安全加固 |
| dcd57d9: 拒绝未知 marker 与重复 wrapper；部署态 v7 自位置验证 | 健壮性 |
| 300187d/f5830dd/ef5a0dd/b852f57: 测试覆盖（v7 re-verification、native KV 拒绝恢复、legacy 迁移 fixture 进镜像 3018 行、digest 记录） | 测试 only |
| 621dd36: decode-floor v7 文档引用；删除过期 concurrent-agents 页 | 文档 |
| f5830dd 附带: legacy 安装 import drift 拒绝 | 防降级 |

启动器（start.sh/start-tp3.sh/start-tp4.sh）**零改动**，fork delta 无接触面。
fork delta 存活核验: raw host-dir 模式、4× ulimit pin、GLM53_DENSE_FP8 转发、
test_tp4_raw_host_dirs.py、.env.tp4.example raw-dir 段、exl3 compact 断言。合并为
clean ort，零冲突。

本地新增环境失败（非回归，pristine upstream 同样失败）:
`tests/test_image_test_layout.py::test_runner_resolves_overlay_and_fixture_in_image_layout`
— macOS `/var` → `/private/var` symlink，runner resolve 后路径前缀不一致；Linux 镜像
内布局不受影响。

## 测试前置

- [ ] spark-eb1a（head）+ 3 workers 各自 `cd /home/colin/nvmodels/GLM-5.3-Flash-EXL3-2x-DGX-Sparks && git fetch origin && git checkout 507b216`（或 push 后的 main tip）
- [ ] `git log --oneline -1`；`bash -n start.sh start-tp3.sh start-tp4.sh`
- [ ] **不要**在生产容器运行期间做任何 GPU 构建/重启

## T1 — stock 路径无回归（最高优先）

1. TP4 启动 boot 日志确认 decode-floor patch 标记为 **v7**（无 v6 残留 marker 告警、
   无 unknown marker / duplicate wrapper 拒绝）
2. `/v1/chat/completions` 冒烟 + warmup canary rc==0；对比合并前 decode receipt 指标
   无漂移（v7 整合了 #246/#221/#180，scheduler 行为有变）
3. canary 拒绝 URL userinfo: 如监控用带 `user:pass@` 的 URL，需确认不被误拒（新安全
   检查），必要时去掉 userinfo
4. raw host-dir 模式复验: `MODEL_HOST_DIR`/`DFLASH_HOST_DIR` 启动 → preflight 通过、
   skip download/rsync 日志在、`:ro` 挂载生效

## T2 — 镜像内测试布局（f5830dd）

下次镜像构建时: `tests/fixtures/legacy_scheduler_helpers.py`（3018 行）随镜像分发，
`docker run <img> python /opt/glm53/test_scheduler_decode_floor.py` 应通过（镜像内
自位置验证，无需挂载 repo）。

## T3 — opt-in 功能（沿上计划，本次未改）

1. TP4 adaptive-k / `GLM53_DEFAULT_REASONING_EFFORT`（#235/#303，见 2026-09-30b T3）
2. GLM53_DENSE_FP8（fork-only）默认 off 不变
3. `EXL3_FAT_GROUPED` 新默认（见 2026-10-01 计划 T1.1）

## T4 — 回退

任一步失败: `git checkout def8fc7`（= 本次 merge 前形态）。decode-floor v7 仅为
overlay patch，回退 checkout 即回 v6 行为；镜像若已 rebuild 需 `docker rmi` 回旧 tag。
