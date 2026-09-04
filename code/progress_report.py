#!/usr/bin/env python3
"""对比两次 benchmark 运行的快照（history/<run_id>/），生成进步观察报告。

用法:  python progress_report.py --history ../history [--old run_X --new run_Y] [--out 文件]
默认取最新的两个快照。输出 history/progress_<old>_vs_<new>.md：
  - 版本集变化（新增/移除版本、commit 变化）
  - 任务级指标对比（Hit@10 / nDCG@10 / PoolRecall，主流程 + etk）
  - 版本级汇总变化
  - 结论（哪些任务改善/退化/持平、哪些版本更新带来了变化）
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRIC_COLS = ["hit10_all", "ndcg10", "pool_recall_all", "prec10"]


def load_metrics(snap: Path):
    p = snap / "metrics.csv"
    if not p.exists():
        return None
    import csv
    rows = {}
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            key = (r["task_id"], r["version_id"], r["track"])
            rows[key] = {c: r.get(c, "") for c in METRIC_COLS}
    return rows


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_old_new(old, new, invert=False):
    """带方向的格式化：0.0 -> 0.125 (+0.125)"""
    if old is None or new is None:
        return "-"
    d = new - old
    if abs(d) < 1e-9:
        return f"{new:.3f} (=)"
    arrow = "↑" if (d > 0) != invert else "↓"
    return f"{new:.3f} ({arrow}{abs(d):.3f})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=str(ROOT / "history"))
    ap.add_argument("--old", default="")
    ap.add_argument("--new", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    hist = Path(args.history)
    snaps = sorted([p for p in hist.glob("run_*") if p.is_dir()])
    if args.old and args.new:
        old_s, new_s = hist / args.old, hist / args.new
    elif len(snaps) >= 2:
        old_s, new_s = snaps[-2], snaps[-1]
    else:
        print(f"history 中快照不足 2 个（{len(snaps)}），跳过对比。")
        return 0

    m_old, m_new = load_metrics(old_s), load_metrics(new_s)
    if m_old is None or m_new is None:
        print("快照缺 metrics.csv，无法对比。")
        return 1

    meta_old = json.load(open(old_s / "run_meta.json")) if (old_s / "run_meta.json").exists() else {}
    meta_new = json.load(open(new_s / "run_meta.json")) if (new_s / "run_meta.json").exists() else {}

    L = []
    L.append(f"# EM-Bench 进步观察：{old_s.name} → {new_s.name}")
    L.append("")
    L.append(f"- 版本集指纹: `{meta_old.get('fingerprint','?')}` → `{meta_new.get('fingerprint','?')}`")
    L.append(f"- 结果: ok={meta_old.get('results',{}).get('ok','?')}/{meta_old.get('results',{}).get('total','?')}"
             f" → ok={meta_new.get('results',{}).get('ok','?')}/{meta_new.get('results',{}).get('total','?')}")
    pv_old, pv_new = meta_old.get("benchmark_version", "?"), meta_new.get("benchmark_version", "?")
    L.append(f"- 协议版本: {pv_old} → {pv_new}")
    if pv_old != pv_new:
        L.append("")
        L.append("> ⚠ **协议版本不同**：两侧指标差异可能来自协议变更（tasks.json "
                 "protocol_changelog，如 1.0 仅传 SMILES → 1.1 传 goal 描述+SMILES），"
                 "不能全部归因于版本本身改进。同协议对比请选取 protocol 版本相同的两次快照。")

    # 版本集变化
    vo, vn = meta_old.get("versions", {}), meta_new.get("versions", {})
    added = [v for v in vn if v not in vo]
    removed = [v for v in vo if v not in vn]
    moved = [v for v in vn if v in vo and vo[v].get("commit") != vn[v].get("commit")]
    L.append("")
    L.append("## 1. 版本集变化")
    L.append(f"- 新增版本: {', '.join(added) if added else '无'}")
    L.append(f"- 移除版本: {', '.join(removed) if removed else '无'}")
    if moved:
        moved_desc = ", ".join(f"{v}: {vo[v].get('commit')}→{vn[v].get('commit')}" for v in moved)
    else:
        moved_desc = "无"
    L.append(f"- commit 前进: {moved_desc}")

    # 任务级对比（按 track 分节）
    tasks = sorted({k[0] for k in m_new} | {k[0] for k in m_old})
    versions = sorted({k[1] for k in m_new} | {k[1] for k in m_old})
    for track, tname in (("primary", "主流程 reaction_full"), ("etk", "enzyme-tk 轨道")):
        keys = [(t, v, track) for t in tasks for v in versions]
        if not any(k in m_old or k in m_new for k in keys):
            continue
        L.append("")
        L.append(f"## 2. {tname} 任务级指标")
        L.append("")
        L.append("| 任务 | 版本 | Hit@10 | nDCG@10 | PoolRecall | Prec@10 |")
        L.append("|---|---|---|---|---|---|")
        for t, v, tr in keys:
            o, n = m_old.get((t, v, tr)), m_new.get((t, v, tr))
            if o is None and n is None:
                continue
            ho, hn = fnum(o["hit10_all"]) if o else None, fnum(n["hit10_all"]) if n else None
            no_, nn = fnum(o["ndcg10"]) if o else None, fnum(n["ndcg10"]) if n else None
            po, pn = fnum(o["pool_recall_all"]) if o else None, fnum(n["pool_recall_all"]) if n else None
            pro, prn = fnum(o["prec10"]) if o else None, fnum(n["prec10"]) if n else None
            L.append(f"| {t} | {v} | {fmt_old_new(ho, hn)} | {fmt_old_new(no_, nn)} "
                     f"| {fmt_old_new(po, pn)} | {fmt_old_new(pro, prn)} |")

    # 汇总：改善/退化计数
    L.append("")
    L.append("## 3. 汇总")
    improved = degraded = 0
    for k in m_new:
        if k not in m_old:
            continue
        for c in METRIC_COLS:
            a, b = fnum(m_old[k][c]), fnum(m_new[k][c])
            if a is None or b is None:
                continue
            if b > a + 1e-9:
                improved += 1
            elif b < a - 1e-9:
                degraded += 1
    L.append(f"任务×版本×指标对中：改善 {improved} 项、退化 {degraded} 项、持平/未变 {sum(1 for k in m_new if k in m_old for c in METRIC_COLS) - improved - degraded} 项（仅计两侧均有的项）。")
    if added:
        L.append(f"新增版本 {', '.join(added)} 的指标请见 §2（旧侧为 '-'）。")
    L.append("")
    L.append("> 注：指标含义与候选池瓶颈的解读见 docs/evaluation_notes.md；"
             "环境/鲁棒性发现见 robustness_findings.md。本文件由 progress_report.py 自动生成。")

    out = Path(args.out) if args.out else hist / f"progress_{old_s.name}_vs_{new_s.name}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"进步报告 -> {out}")


if __name__ == "__main__":
    sys.exit(main())
