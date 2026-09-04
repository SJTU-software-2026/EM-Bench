#!/usr/bin/env python3
"""EM-Bench 单次运行器：在一个版本 worktree 上运行一个任务的固定流水线。

前置条件（由 slurm 脚本保证）：
    已 source code/env.sh（prepare_env 内容，miniprot conda 环境）。
    EM-Bench/versions/<version_id> 已由 setup_versions.sh 准备。

用法：
    python run_task.py --versions versions.json --tasks ../tasks/tasks.json \
        --array-idx N --track primary --results ../results [--timeout 9900]

Slurm array 索引映射：
    track=primary:  idx = 1..len(versions)*len(tasks)，先按版本、再按任务排布
    track=etk:      idx = 1..len(etk_versions)*len(tasks)，跳过无 etk 的版本

产出 results/<version_id>/<task_id>[/__etk]/：
    result.json  统一结果（步骤时间线、候选、排名、资源）
    stdout.log   带时间戳的完整 stdout
    stderr.log
    artifacts/   session.json、ranked.csv、pipeline_summary.json、evidence_memory.json
"""
import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 盲测 lint：goal 文本出现这些模式会向检索链泄漏答案信息（与 evaluate.py
# goal_lint 同一套规则，这里在提交侧提前告警，避免泄漏浪费集群时间）
GOAL_LINT_ENZYME_WORDS = (
    "laccase", "cotA", "tnaA", "tryptophanase", "carbonic anhydrase",
    "petase", "ispetase", "lcc", "cutinase", "aspx", "btnx",
    "haloperoxidase", "chlorinase", "vanillin synthase", "phytase",
)


def goal_lint(goal_text):
    """盲测泄漏 lint：返回触发项列表（空列表 = 通过）。"""
    out = []
    t = goal_text or ""
    if not t.strip():
        return out
    if re.search(r"\bEC\s*\d", t, re.I) or re.search(r"\b\d{1,2}(\.\d{1,2}){3}\b", t):
        out.append("EC 编号疑似泄漏")
    if re.search(r"\b[OPQ][0-9][A-Z0-9]{3}[0-9]\b", t):
        out.append("UniProt accession 疑似泄漏")
    found = [w for w in GOAL_LINT_ENZYME_WORDS if re.search(r"\b" + re.escape(w) + r"\b", t, re.I)]
    if found:
        out.append("酶名疑似泄漏: " + ", ".join(found))
    return out


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[run_task {now_iso()}] {msg}", flush=True)


# ---------------------------------------------------------------- monitoring
class ResourceMonitor(threading.Thread):
    """轮询进程树 RSS 与 GPU 显存；每 interval 秒采样一次。"""

    def __init__(self, proc, pproc, gpu_dev, interval=3.0):
        super().__init__(daemon=True)
        self.proc = proc
        self.pproc = pproc  # psutil.Process(proc.pid)，可能为 None
        self.gpu_dev = gpu_dev
        self.interval = interval
        self.max_rss_mb = 0.0
        self.gpu_peak_mb = 0.0
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def _tree_rss_mb(self):
        if self.pproc is None:
            return 0.0
        total = 0.0
        try:
            procs = [self.pproc] + self.pproc.children(recursive=True)
            for p in procs:
                try:
                    total += p.memory_info().rss / 1e6
                except Exception:
                    pass
        except Exception:
            pass
        return total

    def _gpu_mb(self):
        if not self.gpu_dev:
            return None
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                 "-i", self.gpu_dev],
                capture_output=True, text=True, timeout=5,
            )
            return float(out.stdout.strip().splitlines()[0])
        except Exception:
            return None

    def run(self):
        while not self._stop_evt.wait(self.interval):
            self.max_rss_mb = max(self.max_rss_mb, self._tree_rss_mb())
            g = self._gpu_mb()
            if g is not None:
                self.gpu_peak_mb = max(self.gpu_peak_mb, g)


# ------------------------------------------------------------- artifact I/O
def parse_ranked_csv(path, top_k=20):
    """解析 EnzymeCAGE ranked CSV（列：rank, UniprotID, pred, ...）。"""
    ranked = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                uid = (row.get("UniprotID") or row.get("uniprot_id") or "").strip()
                if not uid:
                    continue
                try:
                    pred = float(row.get("pred") or row.get("score") or "nan")
                except (TypeError, ValueError):
                    pred = None
                ranked.append({
                    "rank": int(float(row.get("rank", len(ranked) + 1))),
                    "uniprot_id": uid,
                    "pred": pred,
                })
                if len(ranked) >= top_k:
                    break
    except Exception as exc:
        print(f"    parse ranked csv warning: {exc}")
    return ranked


def find_json_with_reaction(root_dir, reaction_smiles, t0, t1):
    """在 data/outputs 下找 mtime 落在运行窗口内且 reaction_smiles 匹配的 JSON。"""
    hits = []
    root = Path(root_dir)
    if not root.is_dir():
        return []
    for p in root.rglob("*.json"):
        try:
            st = p.stat()
        except OSError:
            continue
        if not (t0 - 5 <= st.st_mtime <= t1 + 5):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("reaction_smiles") == reaction_smiles:
            hits.append((st.st_mtime, str(p), d))
    return sorted(hits, key=lambda h: h[0])


# ------------------------------------------------------------------- runner
def run_version_task(version, task, track, results_root, timeout, benchmark_version=""):
    version_dir = ROOT / "versions" / version["id"]
    pipeline = "reaction_full" if track == "primary" else "reaction_etk_ec"
    smiles = task["reaction_smiles"]
    # goal 文本（底物级描述，不含酶名/EC/accession）：新版本（v6+）的检索链
    # 会从中提取底物关键词（ABTS/PET 等）做关键词查询与 EC 家族映射；
    # 旧版本只从中提取 SMILES，文本部分对其候选检索无影响（协议兼容）
    goal_text = (task.get("goal_text") or "").strip()
    query = f"{goal_text} {smiles}".strip() if goal_text else smiles
    max_candidates = 20

    gl = goal_lint(goal_text)
    if gl:
        log(f"  !! 盲测 lint 告警（goal_text 疑似泄漏答案信息）: {gl}")

    out_dir = results_root / version["id"] / (task["id"] + ("" if track == "primary" else "__etk"))
    out_dir.mkdir(parents=True, exist_ok=True)
    art_dir = out_dir / "artifacts"
    art_dir.mkdir(exist_ok=True)

    if not version_dir.is_dir():
        (out_dir / "result.json").write_text(json.dumps(
            {"version_id": version["id"], "task_id": task["id"], "track": track,
             "failure_type": "missing_worktree", "error": f"worktree 不存在: {version_dir}"},
            indent=2, ensure_ascii=False))
        return 1

    cmd = [
        sys.executable, "-u", "run.py",
        "-r", query,
        "--phase", "all",
        "--pipeline", pipeline,
        "--max-candidates", str(max_candidates),
        "--no-cache",
    ]
    log(f"{version['id']}/{task['id']} track={track} pipeline={pipeline}")
    log(f"  cmd: {' '.join(cmd)}")

    env = dict(os.environ)
    env.setdefault("CUDA_VISIBLE_DEVICES", "0")
    # run.py 及其子进程（enzymecage/claire/rxnfp）stdout 全缓冲会导致
    # [step] 行在退出时才到达，时间线失真 —— 强制行缓冲
    env["PYTHONUNBUFFERED"] = "1"
    gpu_dev = env.get("CUDA_VISIBLE_DEVICES", "") or None

    t0 = time.time()
    started_at = now_iso()
    proc = subprocess.Popen(
        cmd, cwd=str(version_dir), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    import psutil
    try:
        pproc = psutil.Process(proc.pid)
    except Exception:
        pproc = None
    monitor = ResourceMonitor(proc, pproc, gpu_dev)
    monitor.start()

    stdout_f = open(out_dir / "stdout.log", "w", encoding="utf-8")
    stderr_f = open(out_dir / "stderr.log", "w", encoding="utf-8")

    def pump(stream, sink):
        for line in stream:
            sink.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {line}")
            sink.flush()

    t_out = threading.Thread(target=pump, args=(proc.stdout, stdout_f), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, stderr_f), daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        exit_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        log("  TIMEOUT — 终止进程树")
        try:
            proc.kill()
            if pproc is not None:
                for p in pproc.children(recursive=True):
                    try:
                        p.kill()
                    except Exception:
                        pass
        except Exception:
            pass
        proc.wait(timeout=60)
        exit_code = -9

    t_out.join(timeout=60)
    t_err.join(timeout=60)
    stdout_f.close()
    stderr_f.close()
    monitor.stop()
    monitor.join(timeout=10)
    t1 = time.time()

    wall_s = t1 - t0

    # 定位 session / summary / evidence / ranked（按 mtime 新→旧）
    finds = find_json_with_reaction(version_dir / "data" / "outputs", smiles, t0, t1)
    finds.sort(key=lambda h: h[0], reverse=True)
    session_path, session = None, None
    summary_path, summary = None, None
    evidence_path = None
    for _, path, data in finds:
        if session is None and "/sessions/" in path and data.get("pipeline_id"):
            session_path, session = path, data
        elif summary is None and path.endswith("pipeline_summary.json"):
            summary_path, summary = path, data
        elif evidence_path is None and path.endswith("evidence_memory.json"):
            evidence_path = path

    # 拷贝 artifacts
    def copy_artifact(src, name):
        if not src or not Path(src).is_file():
            return ""
        dst = art_dir / name
        try:
            dst.write_text(Path(src).read_text(encoding="utf-8", errors="replace"))
            return str(dst.relative_to(results_root))
        except Exception:
            return ""

    ranked_csv_abs = (session or {}).get("ranked_csv", "") if session else ""
    ranked = []
    if ranked_csv_abs and Path(ranked_csv_abs).is_file():
        ranked = parse_ranked_csv(ranked_csv_abs)
    elif track == "etk" and session:
        # etk 流程无 ranked CSV：候选顺序即 etk 相似度顺序
        ranked = [{"rank": i + 1, "uniprot_id": u, "pred": None}
                  for i, u in enumerate(session.get("uniprot_ids") or [])]

    # 步骤时间线（从带时间戳的 stdout 解析 "[step] …" 行，支持跨天）
    from datetime import timedelta
    steps = []
    step_re = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d{3})\s+\[(\w+)\]")
    try:
        lines = (out_dir / "stdout.log").read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    start_dt = datetime.fromtimestamp(t0)
    last_ts = None
    cur, cur_dt = None, None
    for line in lines:
        m = step_re.match(line)
        if not m:
            continue
        hms = m.group(1)
        ts = start_dt.replace(hour=int(hms[:2]), minute=int(hms[3:5]),
                              second=int(hms[6:8]),
                              microsecond=int(hms[9:]) * 1000)
        if last_ts is None and ts < start_dt:  # 第一行已在跨天之后
            ts += timedelta(days=1)
        elif last_ts is not None and ts < last_ts:  # 后续跨天
            ts += timedelta(days=1)
        last_ts = ts
        if cur is not None:
            cur["end"] = ts.isoformat(timespec="milliseconds")
            cur["dur_s"] = round((ts - cur_dt).total_seconds(), 2)
            steps.append(cur)
        cur = {"step": m.group(2), "start": ts.isoformat(timespec="milliseconds"),
               "end": None, "dur_s": None, "status": "unknown"}
        cur_dt = ts
    if cur is not None:
        end_dt = datetime.fromtimestamp(t1)
        if end_dt < cur_dt:
            end_dt += timedelta(days=1)
        cur["end"] = end_dt.isoformat(timespec="milliseconds")
        cur["dur_s"] = round((end_dt - cur_dt).total_seconds(), 2)
        steps.append(cur)

    completed = (session or {}).get("completed_steps") or []
    for s in steps:
        s["status"] = "ok" if s["step"] in completed else (
            "failed" if completed else "unknown")

    # 判定失败类型：旧版本 session 可能缺 success 字段或 extra=None（
    # .get 默认值不能取 True，否则 extra 缺失会被误判为成功）；
    # stdout 末尾 "Status: FAILED" 与 Note 行作后备证据
    def session_success(s):
        if s is None:
            return None
        ok = s.get("success")
        if ok is None and isinstance(s.get("extra"), dict):
            ok = s["extra"].get("success")
        return ok

    stdout_text = "\n".join(lines)
    status_failed = "Status: FAILED" in stdout_text
    note = ""
    if status_failed:
        m = re.search(r"Note:\s*(.+)", stdout_text)
        if m:
            note = m.group(1).strip()

    ok_explicit = session_success(session)
    pipeline_success = bool(session) and (
        ok_explicit if ok_explicit is not None else (exit_code == 0 and not status_failed))
    if pipeline_success and status_failed:
        pipeline_success = False  # stdout 明确报 FAILED 时以 stdout 为准

    failure_type = ""
    if timed_out:
        failure_type = "timeout"
    elif session is None:
        failure_type = "no_session"
    elif not pipeline_success:
        failure_type = "pipeline_error"

    # EC 查询线索
    ec_hints = []
    try:
        if evidence_path:
            ev = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
            ec_hints = list(ev.get("queried_ecs") or [])
    except Exception:
        pass
    if not ec_hints and session:
        ec_hints = list((session.get("ec_pool") or {}).keys())

    # 步骤级失败清单（claire/cage 等单步失败但会话仍标记 success 时，
    # failure_type 为空——用 step_failures 单独暴露，避免被 pipeline_success 掩盖）
    step_failures = [s["step"] for s in steps if s["status"] == "failed"]

    result = {
        "benchmark": "EM-Bench",
        "benchmark_version": benchmark_version,
        "version_id": version["id"],
        "version_commit": version.get("commit", ""),
        "search_space": version.get("search_space", ""),
        "task_id": task["id"],
        "task_name": task.get("name", ""),
        "track": track,
        "pipeline": pipeline,
        "phase": "all",
        "reaction_smiles": smiles,
        "goal_text": goal_text,
        "query": query,
        "goal_lint": gl,
        "worktree": str(version_dir),
        "started_at": started_at,
        "finished_at": now_iso(),
        "wall_s": round(wall_s, 1),
        "exit_code": exit_code,
        "timeout": timed_out,
        "pipeline_success": pipeline_success,
        "pipeline_error": (session or {}).get("error") or note,
        "completed_steps": completed,
        "steps": steps,
        "session_json": copy_artifact(session_path, "session.json") if session_path else "",
        "ranked_csv": copy_artifact(ranked_csv_abs, "ranked.csv"),
        "pipeline_summary": copy_artifact(summary_path, "pipeline_summary.json"),
        "evidence_memory": copy_artifact(evidence_path, "evidence_memory.json"),
        "n_candidates": len((session or {}).get("uniprot_ids") or []),
        "candidates": (session or {}).get("uniprot_ids") or [],
        "ranked": ranked,
        "ec_hints": ec_hints,
        "resources": {
            "wall_s": round(wall_s, 1),
            "max_rss_mb": round(monitor.max_rss_mb, 1),
            "gpu_peak_mb": round(monitor.gpu_peak_mb, 1),
        },
        "failure_type": failure_type,
        "step_failures": step_failures,
    }
    out_size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file()) / 1e6
    result["resources"]["out_size_mb"] = round(out_size, 2)
    (out_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False))
    log(f"  done wall={wall_s:.0f}s rss={monitor.max_rss_mb:.0f}MB gpu={monitor.gpu_peak_mb:.0f}MB "
        f"steps={completed} failure={failure_type or '-'}")
    return 0 if failure_type == "" else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", default=str(ROOT / "code" / "versions.json"))
    ap.add_argument("--tasks", default=str(ROOT / "tasks" / "tasks.json"))
    ap.add_argument("--array-idx", type=int, required=True)
    ap.add_argument("--track", choices=["primary", "etk"], default="primary")
    ap.add_argument("--results", default=str(ROOT / "results"))
    ap.add_argument("--timeout", type=int, default=9900)
    ap.add_argument("--skip-existing-success", action="store_true",
                    help="若该任务的 result.json 已存在且 pipeline_success，直接退出 0（失败重试时跳过已成功项）")
    args = ap.parse_args()

    tasks_doc = json.load(open(args.tasks))
    versions = json.load(open(args.versions))["versions"]
    tasks = tasks_doc["tasks"]
    benchmark_version = tasks_doc.get("benchmark_version", "")
    results_root = Path(args.results)

    if args.track == "primary":
        combos = [(v, t) for v in versions for t in tasks]
    else:
        combos = [(v, t) for v in versions if v.get("has_etk") for t in tasks]

    if not 1 <= args.array_idx <= len(combos):
        log(f"array idx {args.array_idx} 超出范围 1..{len(combos)}")
        return 0
    version, task = combos[args.array_idx - 1]
    if args.skip_existing_success:
        res_file = (results_root / version["id"]
                    / (task["id"] + ("" if args.track == "primary" else "__etk"))
                    / "result.json")
        if res_file.exists():
            try:
                if json.load(open(res_file)).get("pipeline_success"):
                    log(f"{version['id']}/{task['id']} track={args.track} 已成功，跳过")
                    return 0
            except (json.JSONDecodeError, OSError):
                pass
    return run_version_task(version, task, args.track, results_root, args.timeout,
                            benchmark_version=benchmark_version)


if __name__ == "__main__":
    sys.exit(main())
