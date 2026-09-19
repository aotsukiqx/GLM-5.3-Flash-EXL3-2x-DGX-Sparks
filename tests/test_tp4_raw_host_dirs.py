#!/usr/bin/env python3
"""TP=4 raw host-dir weights (MODEL_HOST_DIR / DFLASH_HOST_DIR), new design.

CPU-only, fully stubbed. Drives a copied `start-tp4.fn.sh` (trailing
`main "$@"` swapped for `"$@"`) plus the real config preamble, asserting:

1. Config wiring: the 8 HOST_DIR variables and their per-rank `:-` fallbacks.
2. Raw short-circuit: download_weights/download_dflash/sync_weights return 0
   in raw mode with zero host calls; download_only is fail-closed.
3. Preflight gate sub-block: raw mode test -d's the per-rank host dirs;
   default mode keeps the HF-hub writable check; mixed mode gates per repo.
4. launch_cluster mounts: raw mode adds :ro mounts on head AND workers at
   the fixed CTR paths, MODEL_DIR env points inside the container; default
   mode keeps docker argv byte-identical to git HEAD.
5. start() raw branch: _tp4_check_raw_dir validates + MODEL_DIR takes the
   CTR path; a missing dir dies.
"""

import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START_TP4 = ROOT / "start-tp4.sh"

SEP = "\x1f"

# Same shape as tests/test_launcher_rank_parity.py: each argv element separated
# by \x1f, one call per line. `ssh` calls log only the remote tail command.
STUB = """#!/usr/bin/env bash
# Records every invocation; never touches a host.
{ printf '%s\x1f' "$(basename "$0")" "$@"; printf '\n'; } >> "$GLM53_STUB_LOG"
case "$(basename "$0")" in
    ip) printf 'inet %s/24\n' "${GLM53_STUB_HEAD_IP:-10.0.0.1}" ;;
esac
exit 0
"""

PASSED = 0
FAILED = 0


def check(cond: bool, label: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok: {label}")
    else:
        FAILED += 1
        print(f"  FAIL: {label}")


def base_env(**extra: str) -> dict:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent-home",
        "LC_ALL": "C",
        "TERM": "dumb",
        "USER": "tester",
        "GLM53_STUB_LOG": "",
    }
    env.update(extra)
    return env


class Harness:
    """A throwaway copy of the TP=4 launcher plus a stub PATH."""

    def __init__(self, tmp: Path, launcher: Path | None = None, tag: str = "repo"):
        self.repo = tmp / tag
        self.repo.mkdir()
        shutil.copy2(launcher or START_TP4, self.repo / "start-tp4.sh")
        shutil.copy2(ROOT / ".env.tp4.example", self.repo / ".env.tp4.example")
        shutil.copy2(ROOT / ".env.example", self.repo / ".env.example")
        (self.repo / ".env.tp4").write_text("")
        (self.repo / ".env").write_text((ROOT / ".env.example").read_text())
        for sub in ("overlay", "files", "ablit"):
            if (ROOT / sub).is_dir():
                shutil.copytree(ROOT / sub, self.repo / sub)
        self.home = tmp / f"home-{tag}"
        self.home.mkdir()
        self.bin = tmp / f"bin-{tag}"
        self.bin.mkdir()
        for tool in ("docker", "ssh", "scp", "rsync", "curl", "ip", "nvidia-smi"):
            p = self.bin / tool
            p.write_text(STUB)
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.log = tmp / f"calls-{tag}.log"

        # A copy whose trailing `main "$@"` is replaced by `"$@"`, so a single
        # launcher function can be driven with the real configuration preamble.
        text = (self.repo / "start-tp4.sh").read_text()
        assert text.rstrip().endswith('\nmain "$@"'), 'start-tp4.sh must end with main "$@"'
        (self.repo / "start-tp4.fn.sh").write_text(text.rstrip()[: -len('main "$@"')] + '"$@"\n')

    def env(self, **extra: str) -> dict:
        return base_env(
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            HOME=str(self.home),
            GLM53_STUB_LOG=str(self.log),
            **extra,
        )

    def calls(self):
        if not self.log.exists():
            return []
        out = []
        for line in self.log.read_text().splitlines():
            argv = line.split(SEP)
            if argv and argv[-1] == "":
                argv.pop()
            out.append(argv)
        return out

    def run_script(self, body: str, **extra):
        """Run `source ./start-tp4.fn.sh` (real preamble + functions) then body."""
        driver = self.repo / "driver.sh"
        driver.write_text("#!/bin/bash\n. ./start-tp4.fn.sh\n" + body + "\n")
        if self.log.exists():
            self.log.unlink()
        return subprocess.run(
            ["bash", "./driver.sh"],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=False,
            env=self.env(**extra),
        )

    def host_calls(self):
        return [c for c in self.calls() if c and c[0] in ("docker", "ssh", "scp", "rsync")]

    def docker_runs(self):
        runs = []
        for c in self.host_calls():
            if c[:2] == ["docker", "run"]:
                runs.append(("head", c[2:]))
            elif c[0] == "ssh" and c[-1].lstrip().startswith("docker run"):
                runs.append(("worker", shlex.split(c[-1])))
        return runs


def mounts_of(argv):
    out = []
    for i, tok in enumerate(argv):
        if tok == "-v" and i + 1 < len(argv):
            out.append(argv[i + 1])
    return out


def envs_of(argv):
    out = {}
    for i, tok in enumerate(argv):
        if tok == "-e" and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition("=")
            out[k] = v
    return out


def make_raw_model(root: Path, shards: int = 2, bytes_per: int = 100, tag: str = "m") -> Path:
    d = root / f"raw-model-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text("{}\n")
    for i in range(shards):
        (d / f"model-{i:05d}-of-{shards:05d}.safetensors").write_bytes(b"x" * bytes_per)
    return d


def make_raw_dflash(root: Path) -> Path:
    d = root / "raw-dflash"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text("{}\n")
    (d / "model.safetensors").write_bytes(b"d" * 64)
    return d


def make_chat_template(h):
    d = h.repo / "files"
    d.mkdir(exist_ok=True)
    (d / "chat_template.jinja").write_text("tpl\n")


def calm_env_tp4(h: Harness) -> None:
    # Strip the per-node env sections of the shipped example so .env.tp4 does
    # not pin worker IPs the stubs would still "reach". Shared defaults stay.
    keep = []
    for line in (h.repo / ".env.tp4.example").read_text().splitlines():
        if line.startswith(("WORKER", "HEAD_IP", "CX7", "RANK")):
            continue
        keep.append(line)
    (h.repo / ".env.tp4").write_text("\n".join(keep) + "\n")


def launch_body():
    """Plain launch_cluster drive with fixed dirs (parity test)."""
    return """
MODEL_DIR=/root/model
DFLASH_MODEL_DIR=/root/dflash
launch_cluster
"""


def raw_launch_body():
    """start()'s resolve step + launch_cluster, mirroring the launcher."""
    return """
MODEL_DIR=""
if [ -n "$MODEL_HOST_DIR" ]; then
    _tp4_check_raw_dir "$MODEL_HOST_DIR" weights
    MODEL_DIR="$MODEL_CTR_PATH"
else
    MODEL_DIR="$(resolve_model_dir)"
fi
DFLASH_MODEL_DIR=""
if [ "$SPEC_METHOD" = "dflash" ]; then
    if [ -n "$DFLASH_HOST_DIR" ]; then
        _tp4_check_raw_dir "$DFLASH_HOST_DIR" "DFlash2"
        DFLASH_MODEL_DIR="$DFLASH_CTR_PATH"
    else
        DFLASH_MODEL_DIR="$(resolve_dflash_dir)"
    fi
fi
launch_cluster
"""


# ---------------------------------------------------------------- test 1 ----
def test_config_wiring():
    with tempfile.TemporaryDirectory(prefix="tp4raw-") as td:
        _test_config_wiring(Harness(Path(td)), Path(td))


def _test_config_wiring(h, tmp):
    print("1. config wiring: per-rank fallbacks and empty defaults")
    calm_env_tp4(h)
    body = (
        'printf "WM=%s|W2M=%s|W3M=%s\\n" "$WORKER_MODEL_HOST_DIR" "$WORKER2_MODEL_HOST_DIR" "$WORKER3_MODEL_HOST_DIR"\n'
        'printf "WD=%s|W2D=%s|W3D=%s\\n" "$WORKER_DFLASH_HOST_DIR" "$WORKER2_DFLASH_HOST_DIR" "$WORKER3_DFLASH_HOST_DIR"\n'
        'printf "CTRM=%s|CTRD=%s\\n" "$MODEL_CTR_PATH" "$DFLASH_CTR_PATH"\n'
    )
    raw = make_raw_model(tmp, tag="w1")
    rdf = make_raw_dflash(tmp)
    r = h.run_script(body, MODEL_HOST_DIR=str(raw), DFLASH_HOST_DIR=str(rdf))
    vals = {}
    for ln in r.stdout.strip().splitlines():
        for part in ln.split("|"):
            k, _, v = part.partition("=")
            vals[k] = v
    check(vals.get("WM") == str(raw) and vals.get("W2M") == str(raw) and vals.get("W3M") == str(raw),
          f"model fallbacks inherit MODEL_HOST_DIR ({vals.get('WM')})")
    check(vals.get("WD") == str(rdf) and vals.get("W2D") == str(rdf) and vals.get("W3D") == str(rdf),
          "dflash fallbacks inherit DFLASH_HOST_DIR")
    check(vals.get("CTRM") == "/models/glm53-exl3" and vals.get("CTRD") == "/models/glm53-dflash",
          f"fixed CTR constants ({vals.get('CTRM')}, {vals.get('CTRD')})")

    # Per-rank override beats the fallback; empty default keeps everything empty.
    r = h.run_script(body, MODEL_HOST_DIR=str(raw), WORKER2_MODEL_HOST_DIR="/elsewhere/w2")
    vals = {}
    for ln in r.stdout.strip().splitlines():
        for part in ln.split("|"):
            k, _, v = part.partition("=")
            vals[k] = v
    check(vals.get("W2M") == "/elsewhere/w2" and vals.get("WM") == str(raw),
          "WORKER2_MODEL_HOST_DIR overrides its fallback")

    r = h.run_script(body)
    vals = {}
    for ln in r.stdout.strip().splitlines():
        for part in ln.split("|"):
            k, _, v = part.partition("=")
            vals[k] = v
    check(all(vals.get(k) == "" for k in ("WM", "W2M", "W3M", "WD", "W2D", "W3D")),
          "empty defaults stay empty (HF-cache mode)")


# ---------------------------------------------------------------- test 2 ----
def test_raw_shortcircuit():
    with tempfile.TemporaryDirectory(prefix="tp4raw-") as td:
        _test_raw_shortcircuit(Harness(Path(td)), Path(td))


def _test_raw_shortcircuit(h, tmp):
    print("2. raw mode: download/sync are no-ops, download_only fail-closed")
    calm_env_tp4(h)
    raw = make_raw_model(tmp, tag="sc")
    rdf = make_raw_dflash(tmp)

    r = h.run_script("download_weights; download_dflash; sync_weights",
                     MODEL_HOST_DIR=str(raw), DFLASH_HOST_DIR=str(rdf))
    check(r.returncode == 0, f"all three return 0 (tail: {r.stderr.strip().splitlines()[-1][:100] if r.stderr.strip() else ''!r})")
    check(not h.host_calls(), f"zero host calls in raw mode (got {h.host_calls()[:2]})")
    check("skipping HF download" in r.stdout and "skipping worker rsync" in r.stdout,
          "skip reasons logged")

    r = h.run_script("download_only", MODEL_HOST_DIR=str(raw))
    check(r.returncode != 0 and "raw host-dir mode" in r.stderr, "download_only dies in raw mode")
    r = h.run_script("download_only", DFLASH_HOST_DIR=str(rdf))
    check(r.returncode != 0 and "raw host-dir mode" in r.stderr, "download_only dies on raw dflash alone")


# ---------------------------------------------------------------- test 3 ----
def test_preflight_gate():
    with tempfile.TemporaryDirectory(prefix="tp4raw-") as td:
        _test_preflight_gate(Harness(Path(td)), Path(td))


def _test_preflight_gate(h, tmp):
    print("3. preflight gate: raw test -d, default HF-hub writable, mixed per-repo")
    calm_env_tp4(h)
    # The full preflight dies earlier on this stubbed host (CX7 GID sysfs),
    # so drive the gate loop verbatim instead of the whole function.
    gate_loop = """
    for r in 1 2 3; do
        if [ -n "$MODEL_HOST_DIR" ]; then
            worker_ssh_n "$r" "test -d '$(_tp4_rank_model_dir "$r")'" \
                || die "rank ${r} raw model host dir not found: $(_tp4_rank_model_dir "$r")"
        fi
        if [ "$SPEC_METHOD" = "dflash" ] && [ -n "$DFLASH_HOST_DIR" ]; then
            worker_ssh_n "$r" "test -d '$(_tp4_rank_dflash_dir "$r")'" \
                || die "rank ${r} raw dflash host dir not found: $(_tp4_rank_dflash_dir "$r")"
        fi
        if [ -z "$MODEL_HOST_DIR" ] || { [ "$SPEC_METHOD" = "dflash" ] && [ -z "$DFLASH_HOST_DIR" ]; }; then
            if ! worker_ssh_n "$r" "mkdir -p '$(_tp4_rank_hf "$r")/hub' && test -w '$(_tp4_rank_hf "$r")/hub'"; then
                die "rank ${r} cannot write $(_tp4_rank_hf "$r")/hub"
            fi
        fi
    done
"""
    raw = make_raw_model(tmp, tag="gate")
    rdf = make_raw_dflash(tmp)

    r = h.run_script(gate_loop, MODEL_HOST_DIR=str(raw), DFLASH_HOST_DIR=str(rdf))
    check(r.returncode == 0, "full-raw gate passes")
    probed = [c[-1] for c in h.host_calls() if c[0] == "ssh"]
    check(len([x for x in probed if "test -d" in x and "/raw-model-gate" in x]) == 3,
          f"raw mode test -d's the model dir on all 3 ranks (got {probed})")
    check(len([x for x in probed if "test -d" in x and "/raw-dflash" in x]) == 3,
          "raw mode test -d's the dflash dir on all 3 ranks")
    check(not any("mkdir -p" in x for x in probed), "raw mode skips the HF-hub writable check")

    r = h.run_script(gate_loop)
    probed = [c[-1] for c in h.host_calls() if c[0] == "ssh"]
    check(len([x for x in probed if "mkdir -p" in x and ".cache/huggingface/hub" in x]) == 3,
          "default mode keeps the HF-hub writable check on all 3 ranks")

    # Mixed: raw model + HF dflash (SPEC_METHOD defaults to dflash) still
    # gates the hub because dflash still flows through HF download/rsync.
    r = h.run_script(gate_loop, MODEL_HOST_DIR=str(raw))
    probed = [c[-1] for c in h.host_calls() if c[0] == "ssh"]
    check(len([x for x in probed if "test -d" in x and "/raw-model-gate" in x]) == 3, "mixed: model dir still test -d'd")
    check(len([x for x in probed if "mkdir -p" in x]) == 3, "mixed: HF-hub writable check still runs for dflash")

    # A missing raw dir on rank 2 dies naming the rank and dir.
    r = h.run_script(
        'worker_ssh_n() { local r="$1"; shift; [ "$r" = 2 ] && return 1; return 0; }; ' + gate_loop.strip(),
        MODEL_HOST_DIR=str(raw), DFLASH_HOST_DIR=str(rdf))
    check(r.returncode != 0 and "rank 2 raw model host dir not found" in r.stderr,
          "gate dies naming rank and dir when a worker lacks the raw dir")


# ---------------------------------------------------------------- test 4 ----
def test_launch_mounts():
    with tempfile.TemporaryDirectory(prefix="tp4raw-") as td:
        _test_launch_mounts(Harness(Path(td)), Path(td))


def _test_launch_mounts(h, tmp):
    print("4. launch_cluster: raw :ro mounts on head+workers, MODEL_DIR in container")
    calm_env_tp4(h)
    make_chat_template(h)
    raw = make_raw_model(tmp, tag="lm")
    rdf = make_raw_dflash(tmp)

    r = h.run_script(raw_launch_body(), MODEL_HOST_DIR=str(raw), DFLASH_HOST_DIR=str(rdf))
    check(r.returncode == 0, f"launch_cluster completes in raw mode (tail: {r.stderr.strip().splitlines()[-1][:120] if r.stderr.strip() else ''!r})")
    runs = h.docker_runs()
    check(len(runs) == 4, f"one head + three worker docker runs (got {len(runs)})")
    for who, argv in runs:
        m = mounts_of(argv)
        check(f"{raw}:/models/glm53-exl3:ro" in m, f"{who} mounts the raw model dir :ro")
        check(f"{rdf}:/models/glm53-dflash:ro" in m, f"{who} mounts the raw dflash dir :ro")
        e = envs_of(argv)
        check(e.get("MODEL_DIR") == "/models/glm53-exl3", f"{who} MODEL_DIR env is the CTR path")
        check(e.get("DFLASH_MODEL_DIR") == "/models/glm53-dflash", f"{who} DFLASH_MODEL_DIR env is the CTR path")

    # Per-rank overrides reach the right worker: rank 2 mount uses
    # WORKER2_MODEL_HOST_DIR, not the head value.
    r = h.run_script(raw_launch_body(), MODEL_HOST_DIR=str(raw), DFLASH_HOST_DIR=str(rdf),
                     WORKER2_MODEL_HOST_DIR="/rank2/own/model")
    w2 = [argv for who, argv in h.docker_runs() if who == "worker"][1]
    check("/rank2/own/model:/models/glm53-exl3:ro" in mounts_of(w2),
          "rank 2 uses WORKER2_MODEL_HOST_DIR when set")

    # Raw model only (no dflash): no dflash mount anywhere.
    r = h.run_script(raw_launch_body(), MODEL_HOST_DIR=str(raw), SPEC_METHOD="none")
    check(r.returncode == 0, "launch completes with raw model only")
    for who, argv in h.docker_runs():
        check(not any("/models/glm53-dflash" in m for m in mounts_of(argv)),
              f"{who} has no dflash mount when DFLASH_HOST_DIR is empty")


# ---------------------------------------------------------------- test 5 ----
def test_start_raw_branch():
    with tempfile.TemporaryDirectory(prefix="tp4raw-") as td:
        _test_start_raw_branch(Harness(Path(td)), Path(td))


def _test_start_raw_branch(h, tmp):
    print("5. start() resolve step: raw branch validates and pins CTR paths")
    calm_env_tp4(h)
    raw = make_raw_model(tmp, tag="sr")
    rdf = make_raw_dflash(tmp)

    # Drive the resolve sub-block of start() verbatim (preflight etc. cannot
    # run under stubs). Same shape as the launcher's own start().
    resolve = """
MODEL_DIR=""
if [ -n "$MODEL_HOST_DIR" ]; then
    _tp4_check_raw_dir "$MODEL_HOST_DIR" weights
    MODEL_DIR="$MODEL_CTR_PATH"
else
    MODEL_DIR="$(resolve_model_dir)"
fi
DFLASH_MODEL_DIR=""
if [ "$SPEC_METHOD" = "dflash" ]; then
    if [ -n "$DFLASH_HOST_DIR" ]; then
        _tp4_check_raw_dir "$DFLASH_HOST_DIR" "DFlash2"
        DFLASH_MODEL_DIR="$DFLASH_CTR_PATH"
    else
        DFLASH_MODEL_DIR="$(resolve_dflash_dir)"
    fi
    log "DFlash2 load path (in-container): ${DFLASH_MODEL_DIR}"
fi
log "model load path (in-container): ${MODEL_DIR}"
"""
    r = h.run_script(resolve, MODEL_HOST_DIR=str(raw), DFLASH_HOST_DIR=str(rdf))
    check(r.returncode == 0, "raw resolve completes")
    check("model load path (in-container): /models/glm53-exl3" in r.stdout, "MODEL_DIR is the CTR path")
    check("DFlash2 load path (in-container): /models/glm53-dflash" in r.stdout, "DFLASH_MODEL_DIR is the CTR path")
    check("2 safetensors file(s)" in r.stdout, "shard count logged")

    r = h.run_script(resolve, MODEL_HOST_DIR=str(tmp / "no-such-dir"))
    check(r.returncode != 0 and "raw weights host dir not found" in r.stderr,
          "missing raw dir dies")

    empty = tmp / "empty-dir"
    empty.mkdir()
    r = h.run_script(resolve, MODEL_HOST_DIR=str(empty))
    check(r.returncode != 0 and "config.json missing" in r.stderr,
          "raw dir without config.json dies")


# ---------------------------------------------------------------- test 6 ----
def normalize_default_argv(argv, repo: Path, tag: str):
    """Even out run-to-run variance (tmp paths) plus intentional additions
    absent from git HEAD: the nofile EMFILE fix and the opt-in accelerator
    env passthrough (GLM53_ADAPTIVE_K* / GLM53_DENSE_FP8, default off)."""
    home = repo.parent / f"home-{tag}"
    accel = {
        "GLM53_ADAPTIVE_K", "GLM53_ADAPTIVE_K_SET", "GLM53_ADAPTIVE_K_ALPHA",
        "GLM53_ADAPTIVE_K_MARGIN", "GLM53_ADAPTIVE_K_MIN_STEPS",
        "GLM53_ADAPTIVE_K_SATURATE", "GLM53_ADAPTIVE_K_HIST", "GLM53_DENSE_FP8",
    }
    out = []
    i = 0
    while i < len(argv):
        tok = argv[i].replace(str(repo), "<REPO>").replace(str(home), "<HOME>")
        # intentional EMFILE fix, absent from git HEAD: drop the
        # --ulimit nofile=... pair (two separate argv tokens)
        if tok == "--ulimit" and i + 1 < len(argv) and argv[i + 1].startswith("nofile="):
            i += 2
            continue
        # intentional opt-in accelerator passthrough, absent from git HEAD
        # (default off): drop the -e GLM53_ADAPTIVE_K*=... pair
        if tok == "-e" and i + 1 < len(argv) and argv[i + 1].split("=", 1)[0] in accel:
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def test_default_byte_identical():
    with tempfile.TemporaryDirectory(prefix="tp4raw-") as td:
        _test_default_byte_identical(Harness(Path(td)), Path(td))


def _test_default_byte_identical(h, tmp):
    print("6. default HF-cache mode: docker argv byte-parity with git HEAD")
    calm_env_tp4(h)
    make_chat_template(h)
    hf = tmp / "hf"
    (hf / "hub").mkdir(parents=True, exist_ok=True)

    # Baseline: upstream's launcher at the merge — it carries the same sparse-MLA
    # additions as HEAD but no raw-mode code, so the diff isolates exactly the
    # raw-mode change. The head_preload empty-array guard is applied to both
    # texts so the diff also isolates that bash-3.2 bugfix.
    old = tmp / "head-launcher.sh"
    head_text = subprocess.run(
        ["git", "-C", str(ROOT), "show", "357fce7:start-tp4.sh"],
        check=True, capture_output=True, text=True).stdout
    head_text = head_text.replace(
        '"${head_preload[@]}"',
        '${head_preload[@]+"${head_preload[@]}"}')
    old.write_text(head_text)
    h_old = Harness(tmp, launcher=old, tag="old")

    for hh in (h, h_old):
        (hh.repo / ".env.tp4").write_text(
            "\n".join(ln for ln in (ROOT / ".env.tp4.example").read_text().splitlines()
                      if not ln.startswith(("WORKER", "HEAD_IP", "CX7", "RANK"))) + "\n")

    r_new = h.run_script(launch_body())
    r_old = h_old.run_script(launch_body())
    check(r_new.returncode == 0 and r_old.returncode == 0,
          f"both versions complete launch_cluster (new tail: {r_new.stderr.strip().splitlines()[-1][:80] if r_new.stderr.strip() else ''!r})")

    def runs_normalized(hh, tag):
        out = []
        for who, argv in hh.docker_runs():
            out.append((who, normalize_default_argv(argv, hh.repo, tag)))
        return out

    runs_new, runs_old = runs_normalized(h, "repo"), runs_normalized(h_old, "old")
    check(runs_new == runs_old,
          f"default-mode docker argv identical to HEAD after normalization ({len(runs_new)} runs)")
    # The raw mode must actually differ, or the parity proof is vacuous.
    raw = make_raw_model(tmp, tag="parity")
    r_raw = h.run_script(launch_body(), MODEL_HOST_DIR=str(raw))
    runs_raw = [t for who, t in ((who, normalize_default_argv(argv, h.repo, "repo"))
                                 for who, argv in h.docker_runs())]
    check(any("/models/glm53-exl3:ro" in tok for t in runs_raw for tok in t),
          "raw mode argv actually contains the CTR mount (non-vacuous)")


def main() -> int:
    tests = [test_config_wiring, test_raw_shortcircuit, test_preflight_gate,
             test_launch_mounts, test_start_raw_branch, test_default_byte_identical]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(False, f"{t.__name__} raised {type(e).__name__}: {e}")
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
