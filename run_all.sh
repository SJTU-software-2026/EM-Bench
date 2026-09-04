#!/usr/bin/env bash
# EM-Bench 一键全流程：
#   preflight（自检+修复）→ sbatch 提交（每版本×轨道一个数组，版本内 %1 串行）
#   → 轮询等待全部完成 → cage 特征审计 → 评估报告 → 快照到 history/ → 与上次对比生成进步报告
#
# 用法:
#   bash run_all.sh                          # 全部版本、primary+etk 两轨道
#   bash run_all.sh --versions v1_main,v2_etk
#   bash run_all.sh --tracks primary
#   bash run_all.sh --dry-run                # 只打印将执行的命令
#   bash run_all.sh --retry-failed           # 只重跑失败/缺失任务（成功任务自动跳过）
#   bash run_all.sh --skip-wait              # 提交后不等待（手动观察）
#
# 每次运行产出 history/<run_id>/：完整报告快照 + run_meta.json；
# 连续两次运行的指标对比见 history/progress_<旧>_vs_<新>.md。
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
CODE="$ROOT/code"
RESULTS="$ROOT/results"
HISTORY="$ROOT/history"
SLURM_LOGS="$ROOT/slurm_logs"

VERSIONS_ARG=""
TRACKS="both"            # both | primary | etk
DRY_RUN=0
SKIP_PREFLIGHT=0
SKIP_WAIT=0
RETRY_FAILED=0
NO_FIX=0
WAIT_TIMEOUT=21600       # 6h
POLL_INTERVAL=30

usage() { grep '^#   ' "$0" | sed 's/^#   //'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --versions) VERSIONS_ARG="$2"; shift 2 ;;
    --tracks) TRACKS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    --skip-wait) SKIP_WAIT=1; shift ;;
    --retry-failed) RETRY_FAILED=1; shift ;;
    --no-fix) NO_FIX=1; shift ;;
    --wait-timeout) WAIT_TIMEOUT="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知参数: $1"; usage 1 ;;
  esac
done

mkdir -p "$HISTORY" "$SLURM_LOGS" "$RESULTS"

log() { echo "[run_all] $(date '+%H:%M:%S') $*"; }

# ---------------------------------------------------------------- preflight
if [ "$SKIP_PREFLIGHT" = 0 ]; then
  log "preflight 自检（+自动修复符号链接陷阱）..."
  PF_ARGS=()
  [ "$NO_FIX" = 1 ] && PF_ARGS+=(--no-fix)
  [ -n "$VERSIONS_ARG" ] && PF_ARGS+=(--versions "$VERSIONS_ARG")
  if [ "$DRY_RUN" = 0 ]; then
    source "$CODE/env.sh" 2>/dev/null || true
    ROOT="$(cd "$(dirname "$0")" && pwd)"   # env.sh（conda_setup）会覆盖 ROOT，恢复
    CODE="$ROOT/code"; RESULTS="$ROOT/results"; HISTORY="$ROOT/history"
    SLURM_LOGS="$ROOT/slurm_logs"
    python3 "$CODE/preflight.py" "${PF_ARGS[@]}" --out "$RESULTS/preflight.json"
    rc=$?
    if [ "$rc" != 0 ]; then
      log "preflight 存在 fatal 问题（详见 $RESULTS/preflight.json），中止。"
      log "若确认环境已修复可 --skip-preflight 强制运行。"
      exit 1
    fi
  else
    echo "[dry-run] python3 $CODE/preflight.py ${PF_ARGS[*]}"
  fi
fi

# ---------------------------------------------------------------- 计算版本/BASE
# 由 _bases.py 计算各轨道的 (版本 id, BASE, 数组大小)，与 run_task.py 的 combos 顺序一致
BASE_ARGS=()
[ -n "$VERSIONS_ARG" ] && BASE_ARGS+=(--versions "$VERSIONS_ARG")

# ---------------------------------------------------------------- 提交
declare -a JOBS
submit_array() {  # $1=track $2=base $3=版本id $4=sbatch文件 $5=array大小(可选)
  local track="$1" base="$2" vid="$3" sfile="$4" arrn="${5:-8}"
  if [ "$DRY_RUN" = 1 ]; then
    echo "[dry-run] sbatch --export=ALL,BASE=$base,TRACK=$track,ARRAY_N=$arrn $sfile   ($vid)"
    return
  fi
  local out
  out=$(sbatch --export=ALL,BASE="$base",TRACK="$track",ARRAY_N="$arrn" "$sfile")
  local jid
  jid=$(echo "$out" | awk '/Submitted batch job/{print $NF}')
  if [ -n "$jid" ]; then
    JOBS+=("$jid")
    log "提交 $vid [$track] BASE=$base -> job $jid"
  else
    log "!! 提交失败: $out"
  fi
}

if [ "$RETRY_FAILED" = 1 ]; then
  # 全量重放（成功任务被 --skip-existing-success 秒跳），需统计的失败清单由 collect 阶段给出
  # 生成覆盖 --array 与跳过标记的临时 sbatch
  TMP_SBATCH="$SLURM_LOGS/retry_$$.sbatch"
  python3 - "$ROOT/slurm/run_benchmark.sbatch" "$TMP_SBATCH" <<'PYEOF'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
# 覆盖数组大小（由环境变量 ARRAY_N 决定）与跳过开关（先做整行替换，再做数组替换）
text = text.replace(
    'python run_task.py --array-idx "$((SLURM_ARRAY_TASK_ID + BASE))" --track primary --results ../results',
    'python run_task.py --array-idx "$((SLURM_ARRAY_TASK_ID + BASE))" --track "${TRACK}" --results ../results --skip-existing-success')
text = text.replace('--array=1-8%1', '--array=1-${ARRAY_N}%1')
open(dst, "w").write(text)
PYEOF
  log "retry 模式：全量重放数组（已成功任务秒跳）"
  # 与首次提交一致：每版本×轨道一个数组
  if [ "$TRACKS" = "both" ] || [ "$TRACKS" = "primary" ]; then
    while read -r vid base arrn; do
      submit_array primary "$base" "$vid" "$TMP_SBATCH" "$arrn"
    done < <(python3 "$CODE/_bases.py" --track primary "${BASE_ARGS[@]}")
  fi
  if [ "$TRACKS" = "both" ] || [ "$TRACKS" = "etk" ]; then
    while read -r vid base arrn; do
      submit_array etk "$base" "$vid" "$TMP_SBATCH" "$arrn"
    done < <(python3 "$CODE/_bases.py" --track etk "${BASE_ARGS[@]}")
  fi
else
  if [ "$TRACKS" = "both" ] || [ "$TRACKS" = "primary" ]; then
    while read -r vid base arrn; do
      submit_array primary "$base" "$vid" "$ROOT/slurm/run_benchmark.sbatch" "$arrn"
    done < <(python3 "$CODE/_bases.py" --track primary "${BASE_ARGS[@]}")
  fi
  if [ "$TRACKS" = "both" ] || [ "$TRACKS" = "etk" ]; then
    while read -r vid base arrn; do
      submit_array etk "$base" "$vid" "$ROOT/slurm/run_benchmark_etk.sbatch" "$arrn"
    done < <(python3 "$CODE/_bases.py" --track etk "${BASE_ARGS[@]}")
  fi
fi

if [ "$DRY_RUN" = 1 ]; then
  log "dry-run 结束（未实际提交）。"
  exit 0
fi
if [ "${#JOBS[@]}" = 0 ]; then
  log "没有提交任何作业。"
  exit 1
fi

log "已提交 ${#JOBS[@]} 个作业: ${JOBS[*]}"
[ "$SKIP_WAIT" = 1 ] && { log "--skip-wait：不等待，退出。"; exit 0; }

# ---------------------------------------------------------------- 等待
JOBLIST="$(IFS=,; echo "${JOBS[*]}")"
WAITED=0
while :; do
  pending=$(squeue -h -j "$JOBLIST" 2>/dev/null | wc -l)
  [ "$pending" = 0 ] && { log "全部作业完成。"; break; }
  if [ "$WAITED" -ge "$WAIT_TIMEOUT" ]; then
    log "等待超时（${WAIT_TIMEOUT}s），仍有 $pending 个作业在运行；可稍后用 --retry-failed 补跑。"
    exit 2
  fi
  sleep "$POLL_INTERVAL"
  WAITED=$((WAITED + POLL_INTERVAL))
  [ $((WAITED % 300)) -lt "$POLL_INTERVAL" ] && log "等待中… $pending 个作业仍在运行（已等 ${WAITED}s）"
done

# ---------------------------------------------------------------- 收集状态
log "收集结果状态..."
python3 - "$RESULTS" <<'PYEOF'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
fails, missing, ok = [], [], 0
for f in sorted(root.glob("*/t*/result.json")):
    r = json.load(open(f))
    if r.get("pipeline_success"):
        ok += 1
    elif r.get("failure_type"):
        fails.append((f.parts[-2], r.get("failure_type")))
    else:
        fails.append((f.parts[-2], "unknown"))
print(f"ok={ok} fail={len(fails)}")
for v, ft in fails:
    print(f"  FAIL {v} ({ft})")
PYEOF

# ---------------------------------------------------------------- cage 特征审计
log "cage 特征完整性审计..."
source "$CODE/env.sh" 2>/dev/null || true
ROOT="$(cd "$(dirname "$0")" && pwd)"   # env.sh（conda_setup）会覆盖 ROOT，恢复
CODE="$ROOT/code"; RESULTS="$ROOT/results"; HISTORY="$ROOT/history"
SLURM_LOGS="$ROOT/slurm_logs"
python "$CODE/audit_cage_features.py" > "$RESULTS/report/cage_audit.csv" 2>/dev/null || \
  log "!! audit_cage_features.py 失败（不阻塞评估）"

# ---------------------------------------------------------------- 评估
log "生成评估报告..."
python "$CODE/evaluate.py" --results "$RESULTS" --out-dir "$RESULTS/report" || {
  log "!! 评估失败"; exit 3; }

# ---------------------------------------------------------------- LLM 解读（可选）
# 凭据来源（write_notes_llm.py 统一解析）：本仓库 .env（自动加载）或 EMBENCH_LLM_* 环境变量，
# 或 enzyme_update 项目配置（config/settings.yaml、.env、DEEPSEEK_* 环境变量）——共用同一 API
EU_REPO=$(python3 -c "import json;print(json.load(open('$ROOT/code/versions.json')).get('repo',''))" 2>/dev/null)
if [ -n "${EMBENCH_LLM_API_KEY:-}${EMBENCH_LLM_BASE_URL:-}${EMBENCH_LLM_MODEL:-}${DEEPSEEK_API_KEY:-}${DEEPSEEK_BASE_URL:-}${DEEPSEEK_MODEL:-}" ] \
   || [ -f "$ROOT/.env" ] \
   || { [ -n "$EU_REPO" ] && { [ -f "$EU_REPO/config/settings.yaml" ] || [ -f "$EU_REPO/.env" ]; }; }; then
  log "LLM 撰写解读（write_notes_llm.py）..."
  python3 "$CODE/write_notes_llm.py" || log "!! LLM 报告生成失败（不阻塞，可稍后手动重跑）"
else
  log "未配置 LLM API（本仓库 .env / EMBENCH_LLM_* / enzyme_update 配置），跳过 LLM 解读（可用 python code/write_notes_llm.py --dry-run 预览）"
fi

# ---------------------------------------------------------------- 快照
RUN_ID="run_$(date '+%Y%m%d_%H%M%S')"
SNAP="$HISTORY/$RUN_ID"
mkdir -p "$SNAP/figures"
cp -r "$RESULTS/report"/*.md "$RESULTS/report"/*.csv "$SNAP/" 2>/dev/null
cp -r "$RESULTS/report/figures" "$SNAP/" 2>/dev/null
cp "$RESULTS/preflight.json" "$SNAP/preflight.json" 2>/dev/null

python3 - "$SNAP/run_meta.json" "$ROOT" "$RESULTS" "${JOBS[*]}" <<'PYEOF'
import json, sys, hashlib
from pathlib import Path
out, root, results, jobs = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4].split()
versions = json.load(open(root / "code" / "versions.json"))["versions"]
benchmark_version = json.load(open(root / "tasks" / "tasks.json")).get("benchmark_version", "")
fingerprint = hashlib.sha256(
    json.dumps([{k: v for k, v in x.items() if k in ("id", "ref", "commit")} for x in versions],
               sort_keys=True).encode()).hexdigest()[:16]
ok = fail = 0
for f in sorted((results).glob("*/t*/result.json")):
    r = json.load(open(f))
    ok += 1 if r.get("pipeline_success") else 0
    fail += 0 if r.get("pipeline_success") else 1
meta = {
    "run_id": Path(out).parent.name,
    "fingerprint": fingerprint,
    "benchmark_version": benchmark_version,
    "versions": {v["id"]: {"commit": v.get("commit"), "ref": v.get("ref")} for v in versions},
    "slurm_jobs": jobs,
    "results": {"ok": ok, "fail": fail, "total": ok + fail},
}
Path(out).write_text(json.dumps(meta, indent=2, ensure_ascii=False))
print(f"results: ok={ok} fail={fail}")
PYEOF

log "快照 -> $SNAP"

# ---------------------------------------------------------------- 对比历史
log "与上次运行对比..."
python3 "$CODE/progress_report.py" --history "$HISTORY" || log "!! 对比报告生成失败（首次运行无历史属正常）"

log "全部完成。报告: $RESULTS/report/  快照: $SNAP"
