# Deferred spark-node test plan — merge aaa9d64 (upstream main e5761b2, 2026-09-30)

**状态**: 本地（macOS）已完成 merge + `bash -n` + AST parse + pytest 基线 diff
（FAILED 名单与 pre-merge 基线一致，45F 均为 macOS 环境失败：flock 缺失、Xcode
license、bash 3.2 `set -u` 空数组、torch session 污染；非合并回归）。远端 4 台
spark 节点全部运行生产服务，以下步骤**未执行**，待维护窗口由运维按序手动启动。

## 背景：本次合入（upstream main 94ae731 → e5761b2，25 commits）

| PR / 内容 | 性能影响 |
|---|---|
| #235/#302 后续: `start-tp4.sh` 正式转发 `GLM53_ADAPTIVE_K`×7（CLI setness 捕获、`.env.tp4` 显式 opt-in、启用前校验、mode 来源报告、capture-size 扩展生成器；head 经 `nccl_common`，worker 经 `serve_env`） | opt-in（默认 off）。fork 的 bef0bfe 简化 backport 已被其取代（合并时删除 fork 重复块） |
| #299/#303: `GLM53_DEFAULT_REASONING_EFFORT` 在 start-tp3.sh/start-tp4.sh 生效（empty 默认、`low\|high\|max` guard、`--default-chat-template-kwargs` 每 rank、caller export 覆盖 `.env`） | 行为修复（TP3/TP4 之前完全忽略该 knob，无 effort 的请求落回模板 `max`）；默认空 = 不改变现网，操作员显式设置才生效 |
| #302: Dockerfile 把 #289/#203 的 5 条 COPY/RUN 折叠进既有层 — 126 层 → 121 层 | **部署关键修复**: 126 层超 overlay2 ~125 层挂载预算（moby/moby#46740），worker `docker load` 曾 `max depth exceeded`；head 构建正常掩盖了该问题 |
| #203: `overlay/patch_indexer_warmup_range.py` — 大 prefill 中途 JIT spike 抑制（indexer warmup 范围） | runtime perf（TP2 实测修复 mid-serve JIT spike；TP4 行为待测） |
| #206: per-request cached-token 用量上报 | 观测性 |
| #291: preflight GID index 为空时点名常见原因 | 诊断改进 |
| #245: TP3 multimodal worker RPC 走 CX7 | TP3 only |
| 杂项: pack_profile ABLIT_LAYERS 解析对齐 ablit_runtime、FAST MoE field A/B 文档、cache-reporting 文档 | 无运行时影响 |

fork delta 存活核验（本次合并后 vs upstream/main 的 start-tp4.sh diff 仅含）:
raw host-dir 权重模式（MODEL_HOST_DIR/DFLASH_HOST_DIR + per-rank 覆盖 + preflight/sync/download 短路 + :ro 挂载）、
4× `--ulimit nofile=524288:524288`、`GLM53_DENSE_FP8` fork-only 转发（默认 off；in-image `exl3.py` 消费）、
`tests/test_tp4_raw_host_dirs.py`、`.env.tp4.example` raw-dir 段、`tests/test_exl3_overlay.py` compact-selection 断言。

### 合并时的主动清理（fork → upstream 交接）

上游 #235 覆盖了 fork bef0bfe 的全部 ADAPTIVE_K 转发，删除了 fork 三处冗余:
(1) start-tp4.sh 的 7 变量默认块（上游已有 CLI 捕获 + 默认两套机制）；
(2) serve_env 列表重复的 7 项（保留上游一份 + fork-only `GLM53_DENSE_FP8`）；
(3) head docker run 字面 `-e GLM53_ADAPTIVE_K*` 8 行（head 由 `nccl_common` 供给）。
`GLM53_DENSE_FP8` 保留: TP2 `.env` 模板沿用 + head 字面行 + serve_env 行（worker 也读，`exl3.py` 直接消费）。

## 测试前置

- [ ] spark-eb1a（head）+ 3 workers 各自 `cd /home/colin/nvmodels/GLM-5.3-Flash-EXL3-2x-DGX-Sparks && git fetch origin && git checkout aaa9d64`（或 merge 后 main tip）
- [ ] `git log --oneline -1`；`bash -n start.sh start-tp3.sh start-tp4.sh`
- [ ] **不要**在生产容器运行期间做任何 GPU 构建/重启

## T1 — stock 路径无回归（默认形态不变量，最高优先）

1. 不设任何新 knob 启动 TP4（现行 .env.tp4）: boot 日志无 `GLM53_ADAPTIVE_K=… from` 启用行（应打印 disabled/skip）、无 capture-size 生成器运行、无 `--default-chat-template-kwargs` 新增 k=v（effort 为空时）
2. `/v1/chat/completions` 冒烟 + warmup canary rc==0；对比合并前后一次 decode receipt 指标无漂移
3. raw host-dir 模式复验: `MODEL_HOST_DIR`/`DFLASH_HOST_DIR` 启动 → preflight per-rank `test -d` 通过、skip HF download/rsync 日志在、`MODEL_DIR=/models/glm53-exl3` 挂载生效

## T2 — 镜像层折叠（#302）验证（下次镜像构建/分发时）

1. head `docker build` 成功后 `docker history <img> | wc -l` ≤ ~121 层（折叠前 126）
2. worker `docker load` 不再出现 `max depth exceeded`（这是 #302 的直接回归目标）
3. `overlay/patch_indexer_warmup_range.py` 与 `patch_dflash2_exl3` 的 patch 顺序不变（仅层折叠，无语义 diff）— `docker inspect` 对比合并前镜像的 CMD/ENV 一致

## T3 — opt-in 功能（仅维护窗口需要时）

1. TP4 adaptive-k: `.env.tp4` 设 `GLM53_ADAPTIVE_K=ema` → 启动日志出现 `GLM53_ADAPTIVE_K=ema from .env.tp4`、capture-size 扩展、校验通过；错误值（ALPHA=2）在 host 动作前 rc=2 拒绝
2. caller export 优先级: `GLM53_ADAPTIVE_K=off ./start-tp4.sh` 在 `.env.tp4` 设 ema 时应取 off（setness 保留）
3. `GLM53_DEFAULT_REASONING_EFFORT=low` TP4 启动 → 每 rank argv 含 `--default-chat-template-kwargs`，无 effort 请求不再落 `max`
4. GLM53_DENSE_FP8（fork-only）默认 off 不变；如需启用走 TP2 `.env` 既有流程

## T4 — 回退

任一步失败: `git checkout a1c1afb`（= 本次 merge 前形态，40f4ce1+docs）。#302 层折叠未参与时旧镜像不受影响；已 build 的新镜像可 `docker rmi` 回旧 tag。
