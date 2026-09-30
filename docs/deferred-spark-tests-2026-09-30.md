# Deferred spark-node test plan — merge 40f4ce1 (upstream feat/dense-h3-ablit 6227eea, 2026-09-30)

**状态**: 本地（macOS）已完成 merge + `bash -n` + AST parse + pytest（45 FAILED 全部为
macOS 环境失败：flock 缺失、bash 3.2 `set -u` 空数组展开、torch session 污染；纯上游分支
复现同败，非合并回归）。远端 spark 节点全部运行生产服务，以下步骤**未执行**，待维护窗口
由运维按序手动启动。

## 背景：本次合入（1 commit，上游 feature 分支非 main）

上游 main 无新提交（仍在 94ae731）；原作者最新工作在 `feat/dense-h3-ablit`（2026-09-30
06:37 +0800 推送），按技能惯例合入 fork。

| 内容 | 性能影响 |
|---|---|
| `GLM53_MODEL_PRESET=dense-h3` + `ABLIT=1` 时构建并服务 BF16 `o_proj` 变体（layers 15-44 BF16，0-14 保持 EXL3 anchors；变体 ref `glm53-dense-h3-ablit`，独立 build dir/marker，pinned overlay SHA 0623ec34） | 纯 opt-in 构建路径；默认行为不变，对现网 TP2/TP4 运行时零影响 |

改动面：`start.sh`（dense-h3 build 链路）、`tools/dense_overlay.py`（`--keep-bf16
SUFFIX:LAYERS`）、`tools/pack_profile.py`（ABLIT=1 准入 + `--check-ablit`）、
`overlay/ablit_runtime.py`（拒绝文案）、3 个测试文件、`.env.example`、README、CHANGELOG。

### 上游自带的已知缺陷（本地已定位，未修）

`test_ablit_build_keeps_o_proj_bf16_under_its_own_pin` 在 macOS bash 3.2 下失败：
`build_dense_h3` 的 stock 路径 `keep=()` 空数组在 `set -u` 下 `"${keep[@]}"` 展开 →
`keep[@]: unbound variable`（bash < 4.4 行为）。**纯上游分支同败**（worktree 验证），
Linux bash 5（spark 节点、CI）不受影响。基线里有同类既有失败（boot-shape-warmup
`AUTH_ARGS[@]`）。处理：环境失败，不当合并回归、不在 fork 修上游脚本（保持 fork delta 最小），
留待上游修复；若上游长期不修且影响 fork CI，可考虑最小 patch（`keep=("")` + 过滤空串，
或 `local keep; keep=()` + `if [ ${#keep[@]} -gt 0 ]` 分支展开）。

## 测试前置

- [ ] spark-eb1a（head）+ 3 workers 各自 `cd /home/colin/nvmodels/GLM-5.3-Flash-EXL3-2x-DGX-Sparks && git fetch origin && git checkout 40f4ce1`（或 merge 后的 main）
- [ ] 确认 `git log --oneline -1`；`bash -n start.sh start-tp3.sh start-tp4.sh`
- [ ] **不要**在生产容器运行期间做任何 GPU 构建

## T1 — TP2 stock/ABLIT=0 路径无回归（默认形态不变量）

1. 不设 `ABLIT`（或 `ABLIT=0`）以 `GLM53_MODEL_PRESET=dense-h3` 启动（或维持现行 pack）：boot 日志应走 stock 目标 `glm53-dense-h3`，无 `--keep-bf16`，build dir `/.glm53-dense-h3-build`
2. ABLIT=1 + preset：boot 日志出现变体 ref `glm53-dense-h3-ablit`、`--keep-bf16 self_attn.o_proj:15-44`、overlay SHA 校验通过
3. ABLIT=1 但 `GLM53_DENSE_EXL3` 未开/pack 无 BF16 o_proj 时 fail-closed（`--check-ablit` 拒绝，rc=2）
4. `/v1/chat/completions` 冒烟 + warmup canary rc==0

## T2 — 变体构建产物核验（仅首次 ABLIT=1 启动时）

1. 变体 build dir 与 stock 隔离（`-ablit` 后缀）、marker 文件名独立（`overlay-ablit.complete`）
2. staged target 的 `sources` JSON 含 `"keep_bf16":"self_attn.o_proj:15-44"`
3. newest-snapshot fallback 两个 ref（stock + ablit）都被跳过（fallback 永不选 built target）

## T3 — 回退

任一步失败：`git checkout 686f7b1` 即回本次 merge 前形态；变体 build dir 可独立删除，不影响 stock 目标。
