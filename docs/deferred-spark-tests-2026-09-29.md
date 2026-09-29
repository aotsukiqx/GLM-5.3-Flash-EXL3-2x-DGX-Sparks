# Deferred spark-node test plan — merge 686f7b1 (upstream 94ae731, 2026-09-29)

**状态**: 本地（macOS）已完成 merge + bash -n + pytest。远端 spark 节点全部运行生产服务，
以下步骤**未执行**，待环境允许（维护窗口）由运维按序手动启动。

## 背景本次合入（19 commits, 3 PR）

| PR | 内容 | 性能影响 |
|---|---|---|
| #281 | `GLM53_DENSE_FP8=all` + KDA BF16 large-M + 11 GiB KV pool 作为 TP2 模板默认 | decode ms/cycle −4.4~−4.9 %，tok/s +4~5 %，prefill TTFT −12 %，agentic TTFT −4~−10 %；代价 KV 容量 −21.5 %（1.23M tokens，仍 1.45× of 850k）。TP3 保持 opt-in（5ff1b57） |
| #289 | TP2 dense EXL3 H3 target + 6-bpw DFlash2 draft pack 投递（`GLM53_DENSE_EXL3=1`，opt-in） | 新能力：非 routed 模块 EXL3 化（H3 混合 bpw）；TP4 显式拒绝接线（fail-closed） |
| #292 | `GLM53_MODEL_PRESET=dense-h3` 一键构建 H3/6-bpw 对 | 仅 boot 路径（首建 5.3 GB fetch ~66 s + draft 量化）；serving 性能取决于 pack 本身 |

非性能类：198a903 stale-overlay fail-closed（健壮性）、62af50a/3169cef 测试隔离、其余为 docs。

## 测试前置

- [ ] spark-eb1a（head）+ spark worker 各自 `cd /home/colin/nvmodels/GLM-5.3-Flash-EXL3-2x-DGX-Sparks && git fetch origin && git checkout 686f7b1`（或 merge 后的 main）
- [ ] 确认 `git log --oneline -1` == `686f7b1`；`bash -n start.sh start-tp3.sh start-tp4.sh`
- [ ] **不要**在生产容器运行期间做任何 GPU 构建（dense-h3 的 draft build 会拒绝并退出，属预期 fail-closed）

## T1 — TP2 无新特性回归（现行生产形态，不变量测试）

目的：验证 merge 后现行配置（未开 DENSE_EXL3、DENSE_FP8 维持集群 .env 现值）行为不变。

1. 记录当前 `.env` md5；沿用现值启动 `./start.sh`（生产 .env 不动）
2. 检查 boot 日志：`dense fp8 groups: <现值>`、overlay 安装行 `installed exl3.py`、
   `kda bf16-large-m retained` 计数（如 .env 开了 =1）、warmup canary rc==0（`DEGENERATE ENGINE` 不应出现）
3. `/v1/chat/completions` 冒烟：hello 回复正常、DFlash 接受率 > 0（canary 日志）
4. 回归判据：`/metrics` tok/s 与 merge 前基线差在噪声带内（±2 %）

## T2 — PR #281 性能采纳（TP2，需 .env 变更，逐项回滚可退）

> 上游已在 2026-09-26 于其 TP2 生产采纳；数值非劣（3-boot stock-controlled NOT-WORSE）。
> 我们的集群 .env 未随模板自动更新 —— `.env.example` 只是模板。

1. `.env` 三行：`GLM53_DENSE_FP8=all`、`GLM53_KDA_BF16_LARGE_M=1`、
   `--kv-cache-memory-bytes 11811160064`（11 GiB；**先确认 `GLM53_DRAFT_KV_COMPACT=1`**，
   COMPACT=0 时必须保持 14 GiB，否则 850k 请求 boot 拒绝）
2. 重启后核验：KV tokens ≈ 1,233,779、`dense fp8 groups: all`、两 rank 各 ~34 条 `kda bf16-large-m retained`、0 条 NVRM/NV_ERR
3. 基准：跑既有 overnight 套件（A/B/A），判据 decode ms/cycle −4 %以上、prefill TTFT −10 %方向
4. 回滚：恢复 `.env` 三行并 restart（上游 rollback.sh 思路）

## T3 — dense-h3 预设（TP2，可选、纯增量能力）

1. `.env` 加 `GLM53_MODEL_PRESET=dense-h3`（或在命令行传）；首次 start 在 head 构建 H3/6-bpw 对
   （~5.3 GB fetch + GPU draft 量化，需 serve 停止窗口）
2. 构建后 `tools/pack_profile.py` 校验通过、`restart` 不再拒绝
3. 冒烟 + canary + tok/s 对比 T1 基线（H3 混合 bpw 理论带宽收益，上游未给 serving 数字）
4. 回滚：去掉 preset 变量即可（HF cache 中 stage 的 pack 不影响原模型服务）

## T4 — TP4 launcher（仅语法/守卫，无新接线）

1. `./start-tp4.sh` 现行配置 dry 检查：`GLM53_DENSE_EXL3=1` 时必须 rc=2 报
   `not wired on start-tp4.sh`（新 fail-closed 守卫）
2. 正常 TP4 启动路径与 merge 前一致（fork raw-host-dir 模式：`MODEL_HOST_DIR`/`DFLASH_HOST_DIR` 生效，
   merge 冲突解保留了该覆盖逻辑 —— 见 start.sh `DFLASH_HOST_DIR` 块）

## 明确不做（本次 merge 范围外）

- TP3 FP8=all 采纳：上游明示需要独立 A/B/A + numerics study（每 rank 形状不同、FULL capture 需全 rank 一致）
- 在生产运行期间触发任何 pack 构建 / overlay 刷新以外的写操作
