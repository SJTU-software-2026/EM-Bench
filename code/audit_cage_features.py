#!/usr/bin/env python3
"""cage 特征完整性审计：对每个 result.json 统计其 cage 工作目录里的
ESM-C npz / GVP 特征文件数。

背景（鲁棒性发现）：
    enzyme_update 的 cage 特征提取（GVP + ESM-C）在 GPU 异常时可能静默产出
    0 特征：v1_main 有严格检查（n_npz==0 即报错，pipeline FAILED），
    v2+ 删除了该检查——0 特征时 infer 仍可能"成功"并给出排名。
    本脚本在评分之外单独暴露这一维度，供公平性判断与报告引用。

注意：cage 数据因 enzyme_update 的路径拼接缺陷（绝对路径被接到 cwd 下，
    即 "双路径" bug）可能位于 <worktree>/data/outputs/pipeline/<pid>/cage/...
    或 <worktree>/data/gulab/.../data/outputs/pipeline/<pid>/cage/...，
    两处都会扫描。
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"


def cage_dirs_for(result):
    """根据 result.json + session artifact 定位可能的 cage 目录。"""
    wt = Path(result.get("worktree") or "")
    pid = ""
    sess = result.get("session_json") or ""
    sp = RESULTS / sess if sess else None
    if sp and sp.is_file():
        try:
            d = json.loads(sp.read_text(encoding="utf-8"))
            pid = Path(d.get("work_dir") or "").name
        except Exception:
            pass
    bases = []
    if wt.is_dir():
        bases.append(wt / "data" / "outputs" / "pipeline" / pid)
        # 双路径缺陷产物
        doubled = wt / "data" / "gulab"
        if doubled.is_dir():
            for p in doubled.rglob(f"pipeline/{pid}"):
                bases.append(p)
    return bases


def count_features(base):
    npz = gvp = 0
    for c in base.glob("cage/*"):
        npz += len(list(c.rglob("*.npz")))
        gvp += len(list(c.rglob("gvp_protein_feature.pt")))
    return npz, gvp


def main():
    rows = []
    for vdir in sorted(RESULTS.iterdir()):
        if vdir.name in ("first_wave_flakes", "report"):
            continue
        if not vdir.is_dir() or not any(vdir.glob("*/result.json")):
            continue
        for tdir in sorted(vdir.iterdir()):
            rj = tdir / "result.json"
            if not rj.is_file():
                continue
            try:
                result = json.loads(rj.read_text(encoding="utf-8"))
            except Exception:
                continue
            npz, gvp = 0, 0
            for base in cage_dirs_for(result):
                n, g = count_features(base)
                npz, gvp = max(npz, n), max(gvp, g)
            rows.append({
                "version": result.get("version_id", vdir.name),
                "task": tdir.name,
                "track": result.get("track", ""),
                "pipeline_success": result.get("pipeline_success"),
                "failure_type": result.get("failure_type", ""),
                "n_ranked": len(result.get("ranked") or []),
                "esmc_npz": npz,
                "gvp_feat": gvp,
            })
    w = csv.DictWriter(sys.stdout, fieldnames=[
        "version", "task", "track", "pipeline_success", "failure_type",
        "n_ranked", "esmc_npz", "gvp_feat"])
    w.writeheader()
    for r in rows:
        w.writerow(r)


if __name__ == "__main__":
    main()
