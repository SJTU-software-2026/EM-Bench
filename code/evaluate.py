#!/usr/bin/env python3
"""EM-Bench 评估器：聚合所有版本×任务的运行结果，计算检索与资源指标，
生成工具链调用报告、一致性分析、图与最终报告。

指标（干实验适配，参考 deep-research-report 的量化框架与 EC-Bench 的
“性能 + 资源 + 一致性”三维结构；无湿实验活性，等级 y 由锚点 grade 决定：
0=非锚点，2=reference 级锚点，3=discovery/better 级锚点）：

  Hit@k / Precision@k / MRR / nDCG@10 / BestRank / BestGrade@10
  PoolRecall（候选池覆盖） + EF@k（同池随机基线，Monte Carlo 2000 次）
  Spearman rho（CAGE 预测分 vs 等级，仅主流程）
  资源：wall time / 峰值 RSS / GPU 峰值显存 / 输出体积
  一致性：版本间 top-10 集合的成对 Jaccard 与 agreement rate
  综合 100 分（仅 dashboard）：Hit@10 30 + nDCG@10 20 + BestGrade@10 20
      + EF@10 15 + Precision@10 10 + rho 5（资源与效率另表）

用法：python evaluate.py --results ../results [--out-dir ../results/report]
"""
import argparse
import json
import math
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KS = (1, 3, 5, 10)
K_MAIN = 10

# 盲测 lint：goal_text 出现这些模式会向检索链泄漏答案信息（任务锚点酶名，
# 含同家族名与缩写；EC 编号与 UniProt accession 由正则另行检测）
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
    found = [w for w in GOAL_LINT_ENZYME_WORDS if re.search(rf"\b{re.escape(w)}\b", t, re.I)]
    if found:
        out.append("酶名疑似泄漏: " + ", ".join(found))
    return out


# ------------------------------------------------------------------ helpers
def load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def spearman(xs, ys):
    """手工 Spearman rho（避免 scipy 依赖）。"""
    n = len(xs)
    if n < 3:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: (v[i] is None, v[i]))
        r = [0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


def step_tool_map(version_dir: Path):
    """从版本 worktree 的 config/pipelines.yaml 提取 step -> tool 映射。"""
    p = version_dir / "config" / "pipelines.yaml"
    if not p.is_file():
        return {}
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
        return {sid: (sdef or {}).get("tool", "") for sid, sdef in (cfg.get("steps") or {}).items()}
    except Exception:
        return {}


# ------------------------------------------------------------- per-run eval
def grade_of(acc, anchors):
    return anchors.get(acc, {}).get("grade", 0) or 0


def eval_run(res, task, anchors, rng, version=None):
    """返回单次运行的指标 dict；res 可能为 None（失败/缺失）。

    锚点可达性按版本搜索空间（versions.json search_space）计算：
    - swissprot_only：只有 Swiss-Prot 锚点可达（旧版本 reviewed_only=True）；
    - swissprot_trembl_fallback：检索链 Swiss-Prot 优先、空结果自动回退
      TrEMBL（_uniprot_search_adaptive），TrEMBL 锚点按能力计入 reachable。
    是否真的触发回退以本运行候选池中的 TrEMBL 锚点（trEMBL_anchor_in_pool）
    为实证；all-anchor 变体始终是跨版本统一的固定口径。"""
    m = {
        "task_id": task["id"], "version_id": res["version_id"] if res else "?",
        "track": res["track"] if res else "?", "failure_type": res.get("failure_type", "missing") if res else "missing",
        "pipeline_success": bool(res.get("pipeline_success")) if res else False,
        "n_candidates": res.get("n_candidates", 0) if res else 0,
        "n_ranked": len(res.get("ranked", [])) if res else 0,
        "wall_s": res.get("resources", {}).get("wall_s") if res else None,
        "max_rss_mb": res.get("resources", {}).get("max_rss_mb") if res else None,
        "gpu_peak_mb": res.get("resources", {}).get("gpu_peak_mb") if res else None,
        "out_size_mb": res.get("resources", {}).get("out_size_mb") if res else None,
        "completed_steps": ",".join(res.get("completed_steps", [])) if res else "",
        "step_failures": ",".join(res.get("step_failures", [])) if res else "",
    }
    ranked = res.get("ranked", []) if res else []
    pool = res.get("candidates", []) or []
    pool_set = set(pool)

    task_anchors = {a["accession"]: grade_of(a["accession"], anchors) for a in task["anchors"]}
    search_space = (res or {}).get("search_space") or (version or {}).get("search_space", "swissprot_only")
    swiss_reach = {a["accession"] for a in task["anchors"]
                   if anchors.get(a["accession"], {}).get("reachable")}
    if search_space == "swissprot_trembl_fallback":
        # 回退机制按能力覆盖 TrEMBL；是否触发以 trEMBL_anchor_in_pool 实证
        reachable_set = {a["accession"] for a in task["anchors"]}
    else:
        reachable_set = swiss_reach
    has_reachable = len(reachable_set) > 0
    m["search_space"] = search_space
    m["trEMBL_anchor_in_pool"] = ",".join(
        a for a in task_anchors if not anchors.get(a, {}).get("reachable") and a in pool_set)

    anchor_in_pool = {a: (a in pool_set) for a in task_anchors}
    m["pool_recall_reachable"] = (sum(1 for a in reachable_set if a in pool_set) / len(reachable_set)) if has_reachable else None
    m["pool_recall_all"] = (sum(anchor_in_pool.values()) / len(task_anchors)) if task_anchors else None

    top = ranked[:K_MAIN]
    top_ids = [r["uniprot_id"] for r in top]
    m["top10_ids"] = "|".join(top_ids)
    m["top1_id"] = top_ids[0] if top_ids else ""

    def hit_at(k, accs):
        ids = [r["uniprot_id"] for r in ranked[:k]]
        return 1.0 if any(a in ids for a in accs) else 0.0

    def precision_at(k, accs):
        ids = [r["uniprot_id"] for r in ranked[:k]]
        if not ids:
            return 0.0
        return sum(1 for i in ids if i in accs) / len(ids)

    all_accs = set(task_anchors)
    for k in KS:
        m[f"hit{k}_reachable"] = hit_at(k, reachable_set) if has_reachable else None
        m[f"hit{k}_all"] = hit_at(k, all_accs)
        m[f"prec{k}"] = precision_at(k, all_accs)

    # MRR / best rank / best grade in top10
    mrr, best_rank, best_grade10 = 0.0, None, 0
    for i, r in enumerate(ranked[:K_MAIN], start=1):
        if r["uniprot_id"] in all_accs:
            g = task_anchors[r["uniprot_id"]]
            if best_rank is None:
                best_rank = i
                mrr = 1.0 / i
            best_grade10 = max(best_grade10, g)
    m["mrr"] = mrr
    m["best_rank"] = best_rank if best_rank is not None else 0  # 0 = 未命中
    m["best_grade10"] = best_grade10

    # nDCG@10（等级 0/2/3；IDCG 用可达锚点理想排序；无可达锚点则用全部锚点）
    ideal_accs = reachable_set if has_reachable else all_accs
    grades = []
    for r in ranked[:K_MAIN]:
        grades.append(task_anchors.get(r["uniprot_id"], 0))
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(grades))
    ideal_grades = sorted([task_anchors[a] for a in ideal_accs], reverse=True)[:K_MAIN]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal_grades)) if ideal_grades else 0.0
    m["ndcg10"] = (dcg / idcg) if idcg > 0 else 0.0

    # EF@k：同池随机基线（Monte Carlo）
    if pool and ranked:
        k = min(K_MAIN, len(pool))
        n_anchor_pool = sum(anchor_in_pool.values())
        p_base = n_anchor_pool / len(pool)
        m["ef_base10"] = (precision_at(K_MAIN, all_accs) / p_base) if p_base > 0 else None
        rand_prec_sum = rand_hit_sum = 0.0
        N = 2000
        for _ in range(N):
            sample = rng.sample(pool, k)
            n_hit = sum(1 for a in sample if a in all_accs)
            rand_prec_sum += n_hit / k
            rand_hit_sum += 1.0 if n_hit > 0 else 0.0
        m["rand_prec10"] = rand_prec_sum / N
        m["rand_hit10"] = rand_hit_sum / N
        m["ef_mc10"] = (precision_at(K_MAIN, all_accs) / m["rand_prec10"]) if m["rand_prec10"] > 0 else None
    else:
        m["ef_base10"] = m["rand_prec10"] = m["rand_hit10"] = m["ef_mc10"] = None

    # Spearman：CAGE 预测分 vs 等级（主流程）
    if res and res.get("track") == "primary" and len(top) >= 3:
        xs = [r.get("pred") for r in top]
        ys = [task_anchors.get(r["uniprot_id"], 0) for r in top]
        m["spearman_pred_grade"] = spearman(xs, ys)
    else:
        m["spearman_pred_grade"] = None

    m["anchor_variant"] = "reachable" if has_reachable else "all"
    return m


# ------------------------------------------------------------ aggregation
def composite_score(rows):
    """100 分 dashboard 分（干实验适配，权重说明见 README）。"""
    def mean(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    hit10 = mean("hit10_primary")
    ndcg = mean("ndcg10")
    bg10 = mean("best_grade10") / 3.0
    ef = mean("ef_mc10")
    ef_norm = min(ef, 5.0) / 5.0 if ef else 0.0
    prec = mean("prec10")
    rho_vals = [r["spearman_pred_grade"] for r in rows if r.get("spearman_pred_grade") is not None]
    rho = (sum(rho_vals) / len(rho_vals)) if rho_vals else 0.0
    return {
        "hit10": hit10 * 30,
        "ndcg10": ndcg * 20,
        "best_grade10": bg10 * 20,
        "ef10": ef_norm * 15,
        "prec10": prec * 10,
        "rho": ((rho + 1) / 2) * 5,
        "total": hit10 * 30 + ndcg * 20 + bg10 * 20 + ef_norm * 15 + prec * 10 + ((rho + 1) / 2) * 5,
    }


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "results"))
    ap.add_argument("--out-dir", default=str(ROOT / "results" / "report"))
    ap.add_argument("--no-figures", action="store_true")
    args = ap.parse_args()

    results_root = Path(args.results)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(exist_ok=True)

    tasks = load_json(ROOT / "tasks" / "tasks.json", {})["tasks"]
    anchors = load_json(ROOT / "tasks" / "anchors.json", {})
    versions = load_json(ROOT / "code" / "versions.json", {})["versions"]

    rng = random.Random(42)

    goal_lints = {t["id"]: goal_lint(t.get("goal_text") or "") for t in tasks}

    # ---- 逐运行评估
    rows = []
    per_task_version = {}
    for v in versions:
        vdir = ROOT / "versions" / v["id"]
        toolmap = step_tool_map(vdir)
        for t in tasks:
            for track in ("primary", "etk"):
                if track == "etk" and not v.get("has_etk"):
                    continue
                res_path = results_root / v["id"] / (t["id"] + ("" if track == "primary" else "__etk")) / "result.json"
                res = load_json(res_path)
                m = eval_run(res, t, anchors, rng, version=v)
                m["goal_lint"] = ",".join(goal_lints.get(t["id"], []))
                m["pipeline"] = "reaction_full" if track == "primary" else "reaction_etk_ec"
                m["toolchain"] = ""
                if res:
                    toolchain = []
                    for s in res.get("steps", []):
                        tool = toolmap.get(s["step"], "?")
                        toolchain.append(f"{s['step']}({tool},{s.get('status','?')},{s.get('dur_s')}s)")
                    m["toolchain"] = " -> ".join(toolchain)
                    m["ec_hints"] = ",".join(res.get("ec_hints", []) or [])
                    m["worktree"] = res.get("worktree", "")
                else:
                    m["ec_hints"] = ""
                per_task_version[(t["id"], v["id"], track)] = (m, res)
                rows.append(m)

    # ---- 汇总表
    import csv as _csv
    metrics_csv = out_dir / "metrics.csv"
    fieldnames = [k for k in rows[0].keys()]
    with open(metrics_csv, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # 版本 × 轨道汇总
    def summarize(rows_subset):
        s = {"n_runs": len(rows_subset), "n_fail": sum(1 for r in rows_subset if r["failure_type"] != ""),
             "n_success": sum(1 for r in rows_subset if r["pipeline_success"])}
        for k in KS:
            vals = [r[f"hit{k}_{r['anchor_variant']}"] for r in rows_subset
                    if r.get(f"hit{k}_{r['anchor_variant']}") is not None]
            s[f"hit{k}"] = round(sum(vals) / len(vals), 3) if vals else None
        for key in ("ndcg10", "best_grade10", "prec10", "ef_mc10", "rand_hit10",
                    "pool_recall_all", "spearman_pred_grade"):
            vals = [r[key] for r in rows_subset if r.get(key) is not None]
            s[key] = round(sum(vals) / len(vals), 3) if vals else None
        walls = [r["wall_s"] for r in rows_subset if r.get("wall_s")]
        rss = [r["max_rss_mb"] for r in rows_subset if r.get("max_rss_mb")]
        gpu = [r["gpu_peak_mb"] for r in rows_subset if r.get("gpu_peak_mb")]
        s["wall_h_total"] = round(sum(walls) / 3600, 2) if walls else None
        s["max_rss_mb_max"] = round(max(rss), 0) if rss else None
        s["gpu_peak_mb_max"] = round(max(gpu), 0) if gpu else None
        return s

    ver_rows = []
    for v in versions:
        prim = [r for r in rows if r["version_id"] == v["id"] and r["track"] == "primary"]
        etk = [r for r in rows if r["version_id"] == v["id"] and r["track"] == "etk"]
        s = summarize(prim)
        s["version"] = v["id"]
        s["date"] = v["date"]
        s["label"] = v["label"]
        s["track"] = "primary"
        ver_rows.append(s)
        if etk:
            s2 = summarize(etk)
            s2.update({"version": v["id"], "date": v["date"], "label": v["label"], "track": "etk"})
            ver_rows.append(s2)
    with open(out_dir / "summary_by_version.csv", "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=list(ver_rows[0].keys()))
        w.writeheader()
        w.writerows(ver_rows)

    task_rows = []
    for t in tasks:
        prim = [r for r in rows if r["task_id"] == t["id"] and r["track"] == "primary"]
        s = summarize(prim)
        s["task"] = t["id"]
        s["name"] = t["name"]
        s["difficulty"] = t["difficulty"]
        n_anchor_reach = sum(1 for a in t["anchors"] if anchors.get(a["accession"], {}).get("reachable"))
        s["n_anchors"] = len(t["anchors"])
        s["n_reachable_anchors"] = n_anchor_reach
        task_rows.append(s)
    with open(out_dir / "summary_by_task.csv", "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=list(task_rows[0].keys()))
        w.writeheader()
        w.writerows(task_rows)

    # 一致性：主流程 top-10 成对 Jaccard（按任务，再取平均）
    consistency = []
    for t in tasks:
        sets = {}
        for v in versions:
            r = per_task_version.get((t["id"], v["id"], "primary"), (None, None))[0]
            if r:
                ids = [x for x in (r.get("top10_ids") or "").split("|") if x]
                sets[v["id"]] = set(ids)
        for i, v1 in enumerate(versions):
            for v2 in versions[i + 1:]:
                if v1["id"] in sets and v2["id"] in sets:
                    a, b = sets[v1["id"]], sets[v2["id"]]
                    j = len(a & b) / len(a | b) if (a | b) else 0.0
                    consistency.append({"task": t["id"], "v1": v1["id"], "v2": v2["id"], "jaccard": round(j, 3)})
    with open(out_dir / "consistency.csv", "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=["task", "v1", "v2", "jaccard"])
        w.writeheader()
        w.writerows(consistency)
    if consistency:
        mean_j = sum(c["jaccard"] for c in consistency) / len(consistency)
        print(f"平均版本间 top-10 一致率 (Jaccard): {mean_j:.3f}")

    # 综合分
    comp_rows = []
    for v in versions:
        prim = [r for r in rows if r["version_id"] == v["id"] and r["track"] == "primary"]
        c = composite_score(prim)
        c["version"] = v["id"]
        c["date"] = v["date"]
        comp_rows.append(c)
    with open(out_dir / "composite.csv", "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=list(comp_rows[0].keys()))
        w.writeheader()
        w.writerows(comp_rows)

    # ---- 工具链报告（核心交付物：每个挖掘工作 × 每个版本的具体工具链调用）
    write_toolchain_report(out_dir, versions, tasks, anchors, per_task_version)

    # ---- 报告
    write_report(out_dir, versions, tasks, ver_rows, task_rows, comp_rows, consistency)

    # ---- 图
    if not args.no_figures:
        try:
            make_figures(fig_dir, tasks, versions, per_task_version, ver_rows, comp_rows, consistency)
        except Exception as exc:
            print(f"[warn] 图生成失败: {exc}")

    print(f"评估完成 -> {out_dir}")


# ------------------------------------------------------- toolchain report
def write_toolchain_report(out_dir, versions, tasks, anchors, ptv):
    lines = []
    lines.append("# EM-Bench 工具链调用报告\n")
    lines.append("每个挖掘工作（任务）× 每个版本：实际执行的固定流水线步骤（工具）、状态、"
                 "耗时、EC 查询线索、候选池与 top-10 排名（含锚点标注）。\n")
    lines.append("> 工具链为 run.py 固定流水线（`--phase all`）的实际执行记录，"
                 "由 EM-Bench 运行器从 stdout 时间线与会话 JSON 恢复。\n")

    # 版本能力矩阵
    lines.append("## 版本能力矩阵（config/pipelines.yaml + versions.json）\n")
    lines.append("| 版本 | 提交 | 日期 | 搜索空间 | reaction_full 步骤 | etk 流程 | reflect | 定向进化 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for v in versions:
        vdir = ROOT / "versions" / v["id"]
        steps = ""
        try:
            import yaml
            cfg = yaml.safe_load((vdir / "config" / "pipelines.yaml").read_text(encoding="utf-8"))
            steps = ",".join((cfg.get("pipelines") or {}).get("reaction_full", {}).get("steps", []))
        except Exception:
            steps = "?"
        ss = v.get("search_space", "swissprot_only")
        ss_txt = "Swiss-Prot" if ss == "swissprot_only" else "Swiss-Prot→TrEMBL 回退"
        lines.append(f"| {v['id']} | {v['commit']} | {v['date']} | {ss_txt} | {steps} | "
                     f"{'yes' if v.get('has_etk') else 'no'} | {'yes' if v.get('has_reflect') else 'no'} | "
                     f"{'yes' if v['id'] == 'v5_evolve' else 'no'} |")
    lines.append("")

    for t in tasks:
        lines.append(f"## {t['id']} — {t['name']}（难度 {t['difficulty']}）\n")
        lines.append(f"- 来源: {t['source']}")
        lines.append(f"- 反应: `{t['reaction_smiles']}`")
        lines.append(f"- 文献条件: {t['paper_conditions']}")
        lines.append(f"- 检测: {t['detection']}")
        anchor_desc = ", ".join(
            f"{a['accession']}(grade {a['grade']}, {a['label']}, "
            f"{'Swiss-Prot' if anchors.get(a['accession'], {}).get('reachable') else 'TrEMBL（仅 fallback 版本可达）'})"
            for a in t["anchors"])
        lines.append(f"- 锚点: {anchor_desc}")
        lines.append("")
        lines.append("| 版本 | 工具链（step→工具, 状态, 耗时） | EC 线索 | 候选数 | top-1 | top-10 锚点命中 | 失败 |")
        lines.append("|---|---|---|---|---|---|---|")
        for v in versions:
            m, res = ptv.get((t["id"], v["id"], "primary"), (None, None))
            if m is None:
                lines.append(f"| {v['id']} | 未运行 | | | | | |")
                continue
            ids = [x for x in (m.get("top10_ids") or "").split("|") if x]
            accs = {a["accession"] for a in t["anchors"]}
            hits = [x for x in ids if x in accs]
            hit_str = ",".join(f"**{x}**" for x in hits) if hits else "—"
            fail = m["failure_type"] or ("ok" if m["pipeline_success"] else "pipeline_error")
            lines.append(f"| {v['id']} | {m['toolchain'] or '—'} | {m.get('ec_hints') or '—'} | "
                         f"{m['n_candidates']} | `{m.get('top1_id') or '—'}` | {hit_str} | {fail} |")
        lines.append("")
        # etk 轨道
        etk_versions = [v for v in versions if v.get("has_etk")]
        if any(ptv.get((t["id"], v["id"], "etk"))[0] is not None for v in etk_versions):
            lines.append("**enzyme-tk 轨道（reaction_etk_ec，候选顺序即 etk 相似度顺序）：**\n")
            lines.append("| 版本 | 工具链 | EC 线索 | 候选数 | top-1 | top-10 锚点命中 | 失败 |")
            lines.append("|---|---|---|---|---|---|---|")
            for v in etk_versions:
                m, _ = ptv.get((t["id"], v["id"], "etk"), (None, None))
                if m is None:
                    continue
                ids = [x for x in (m.get("top10_ids") or "").split("|") if x]
                accs = {a["accession"] for a in t["anchors"]}
                hits = [x for x in ids if x in accs]
                hit_str = ",".join(f"**{x}**" for x in hits) if hits else "—"
                fail = m["failure_type"] or ("ok" if m["pipeline_success"] else "pipeline_error")
                lines.append(f"| {v['id']} | {m['toolchain'] or '—'} | {m.get('ec_hints') or '—'} | "
                             f"{m['n_candidates']} | `{m.get('top1_id') or '—'}` | {hit_str} | {fail} |")
            lines.append("")
    (out_dir / "toolchain.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"工具链报告 -> {out_dir / 'toolchain.md'}")


# ----------------------------------------------------------------- report
def write_report(out_dir, versions, tasks, ver_rows, task_rows, comp_rows, consistency):
    L = []
    L.append("# EM-Bench：enzyme_update 多版本酶挖掘 Benchmark 评估报告\n")
    L.append("## 1. 设计与数据\n")
    L.append("参照 EC-Bench（统一平台 / 标准化任务与数据 / 性能+资源+一致性三维指标 / 可扩展）"
             "与 deep-research-report 的 prospective top-k 酶挖掘评价框架，对 enzyme_update "
             f"的 {len(versions)} 个历史版本在 {len(tasks)} 个文献挖掘任务上做标准化评测。")
    L.append("")
    L.append("| 版本 | 日期 | 说明 |")
    L.append("|---|---|---|")
    for v in versions:
        L.append(f"| {v['id']} | {v['date']} | {v['label']} |")
    L.append("")
    L.append("任务锚点按 grade 分级：2 = 论文 reference 酶，3 = 论文挖掘得到的 discovery 酶"
             "（如 AspX/BtnX）。无湿实验活性数据，等级 y 为干实验代理（0/2/3）。")
    L.append("候选搜索空间按版本记录（versions.json search_space，代码实证见各版本"
             " src/orchestration/executor.py `_uniprot_search_adaptive`）：")
    L.append("")
    for v in versions:
        ss = v.get("search_space", "swissprot_only")
        desc = ("仅 Swiss-Prot（reviewed_only=True）" if ss == "swissprot_only"
                else "Swiss-Prot 优先，空结果自动回退 TrEMBL")
        L.append(f"- {v['id']}: {ss}（{desc}）")
    L.append("")
    L.append("TrEMBL 锚点（A0AAC9SM19/A8LT50/Q7SIG1）对 swissprot_only 版本不可达；"
             "对 swissprot_trembl_fallback 版本按能力计入 reachable 变体。"
             "各运行的 `trEMBL_anchor_in_pool` 字段记录 TrEMBL 锚点实际进入候选池的实证；"
             "all-anchor 变体始终为跨版本统一的固定口径。")
    L.append("")
    lint_rows = [(t["id"], goal_lint(t.get("goal_text") or "")) for t in tasks]
    if any(l for _, l in lint_rows):
        L.append("**盲测 lint 告警**（goal_text 疑似泄漏，见 metrics.csv goal_lint 列）：")
        for tid, lints in lint_rows:
            if lints:
                L.append(f"- {tid}: {'; '.join(lints)}")
    else:
        L.append("盲测 lint：全部任务 goal_text 通过（无 EC 编号 / UniProt accession / 锚点酶名泄漏）。")
    L.append("")

    L.append("## 2. 版本 × 任务指标总表（主流程 reaction_full）\n")
    L.append("| 版本 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | nDCG@10 | Prec@10 | EF@10(MC) | "
             "BestGrade@10 | PoolRecall | rho | 成功/失败 | 总墙钟(h) | 峰值RSS(MB) | GPU峰值(MB) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in ver_rows:
        if r["track"] != "primary":
            continue
        L.append(f"| {r['version']} | {r.get('hit1')} | {r.get('hit3')} | {r.get('hit5')} | {r.get('hit10')} "
                 f"| {r.get('ndcg10')} | {r.get('prec10')} | {r.get('ef_mc10')} | {r.get('best_grade10')} "
                 f"| {r.get('pool_recall_all')} | {r.get('spearman_pred_grade')} | "
                 f"{r['n_success']}/{r['n_fail']} | {r.get('wall_h_total')} | {r.get('max_rss_mb_max')} "
                 f"| {r.get('gpu_peak_mb_max')} |")
    L.append("")
    if any(r["track"] == "etk" for r in ver_rows):
        L.append("### enzyme-tk 轨道（reaction_etk_ec）\n")
        L.append("| 版本 | Hit@10 | nDCG@10 | Prec@10 | EF@10(MC) | PoolRecall | 成功/失败 |")
        L.append("|---|---|---|---|---|---|---|")
        for r in ver_rows:
            if r["track"] != "etk":
                continue
            L.append(f"| {r['version']} | {r.get('hit10')} | {r.get('ndcg10')} | {r.get('prec10')} "
                     f"| {r.get('ef_mc10')} | {r.get('pool_recall_all')} | {r['n_success']}/{r['n_fail']} |")
        L.append("")

    L.append("## 3. 任务难度画像\n")
    L.append("| 任务 | 难度 | 锚点(Swiss-Prot 基线/总数) | 版本平均 Hit@10 | 随机基线 Hit@10 | 版本平均 EF@10 | 成功运行 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in task_rows:
        L.append(f"| {r['task']} ({r['name'][:38]}) | {r['difficulty']} | "
                 f"{r['n_reachable_anchors']}/{r['n_anchors']} | {r.get('hit10')} | {r.get('rand_hit10')} "
                 f"| {r.get('ef_mc10')} | {r['n_success']}/{r['n_fail']} |")
    L.append("")

    L.append("## 4. 综合评分（100 分 dashboard，权重见 README；只作概览，正式比较用原始指标）\n")
    L.append("| 版本 | Hit@10×30 | nDCG×20 | BestGrade×20 | EF×15 | Prec×15 | rho×5 | 总分 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for c in comp_rows:
        L.append(f"| {c['version']} | {c['hit10']:.1f} | {c['ndcg10']:.1f} | {c['best_grade10']:.1f} "
                 f"| {c['ef10']:.1f} | {c['prec10']:.1f} | {c['rho']:.1f} | **{c['total']:.1f}** |")
    L.append("")

    L.append("## 5. 版本间一致性\n")
    if consistency:
        mean_j = sum(c["jaccard"] for c in consistency) / len(consistency)
        L.append(f"主流程 top-10 候选集合的成对 Jaccard 均值 = **{mean_j:.3f}**"
                 f"（{len(consistency)} 个 任务×版本对，见 consistency.csv）。")
    L.append("")
    L.append("## 6. 局限与注意事项\n")
    L.append("1. 干实验 benchmark：无湿实验活性，等级 y 为锚点 grade 代理；BRA/活性相关性需后续实验数据。")
    L.append("2. swissprot_only 版本搜不到 TrEMBL 锚点（AspX/BtnX/Q7SIG1）；"
             "swissprot_trembl_fallback 版本的 reachable 变体按能力纳入 TrEMBL 锚点，"
             "是否真的触发回退以 trEMBL_anchor_in_pool 实证为准；all-anchor 变体为跨版本统一口径。")
    L.append("3. t1 用 ABTS 形式化 SMILES 对、t5 用 BHET 二聚体替代聚合物；t4/t5 的文献锚点"
             "（SsCSO/CSO2/KbPETase）不在 UniProtKB，无法作为检索锚点。")
    L.append("4. 单次运行未做重复；时间/资源指标为单次观测。")
    L.append("5. 版本间代码与默认参数不同属预期（版本演化即评测对象）；跨版本比较需结合工具链报告解读。")
    L.append("")
    L.append("详细逐运行指标见 metrics.csv；工具链调用见 toolchain.md。")
    L.append("")
    L.append("补充章节（手工维护，不在本文件内）："
             "鲁棒性发现见 robustness_findings.md；评价解读见 evaluation_notes.md。")
    (out_dir / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"报告 -> {out_dir / 'report.md'}")


# ---------------------------------------------------------------- figures
def make_figures(fig_dir, tasks, versions, ptv, ver_rows, comp_rows, consistency):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 9, "figure.dpi": 150})

    def hit10_of(vid, tid):
        m, _ = ptv.get((tid, vid, "primary"), (None, None))
        if m is None:
            return 0.0
        return m.get(f"hit10_{m['anchor_variant']}") or 0.0

    # fig1: Hit@10 柱状图（任务 × 版本）
    fig, ax = plt.subplots(figsize=(10, 4.5))
    tids = [t["id"] for t in tasks]
    width = 0.16
    cols = plt.cm.viridis([i / (len(versions) - 1) for i in range(len(versions))])
    for i, v in enumerate(versions):
        vals = [hit10_of(v["id"], t) for t in tids]
        ax.bar([x + i * width for x in range(len(tids))], vals, width,
               label=v["id"], color=cols[i])
    ax.set_xticks([x + width * (len(versions) - 1) / 2 for x in range(len(tids))])
    ax.set_xticklabels(tids, rotation=30, ha="right")
    ax.set_ylabel("Hit@10 (anchor variant)")
    ax.set_title("EM-Bench: Hit@10 by task and version (primary track)")
    ax.legend(ncol=5, fontsize=7)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig1_hit10.png")
    plt.close(fig)

    # fig2: 工具链时间线（每任务一张子图）
    ncols = 4
    nrows = math.ceil(len(tasks) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(14, 2.6 * nrows), squeeze=False)
    for ti, t in enumerate(tasks):
        ax = axes[ti // ncols][ti % ncols]
        ax.set_title(t["id"], fontsize=8)
        yticks, ylabels = [], []
        for vi, v in enumerate(versions):
            m, res = ptv.get((t["id"], v["id"], "primary"), (None, None))
            if res is None:
                continue
            x = 0.0
            for s in res.get("steps", []):
                d = s.get("dur_s") or 0.0
                color = "C2" if s.get("status") == "ok" else ("C3" if s.get("status") == "failed" else "C7")
                ax.barh(vi, d, left=x, height=0.55, color=color, edgecolor="white", linewidth=0.4)
                if d > 3:
                    ax.text(x + d / 2, vi, s["step"], ha="center", va="center", fontsize=5.5)
                x += d
            yticks.append(vi)
            ylabels.append(v["id"])
        ax.set_yticks(yticks)
        ax.set_yticklabels(ylabels, fontsize=6)
        ax.set_xlabel("seconds", fontsize=7)
    for ax in axes.flat[len(tasks):]:
        ax.axis("off")
    fig.suptitle("EM-Bench: toolchain step timelines per task (green=ok, red=failed, grey=unknown)", y=1.0)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig2_toolchain_timelines.png")
    plt.close(fig)

    # fig3: 资源散点（wall time vs peak RSS，颜色=版本，形状=轨道）
    fig, ax = plt.subplots(figsize=(7, 5))
    for i, v in enumerate(versions):
        for track, mk in (("primary", "o"), ("etk", "s")):
            xs, ys = [], []
            for t in tasks:
                m, res = ptv.get((t["id"], v["id"], track), (None, None))
                if m and m.get("wall_s"):
                    xs.append(m["wall_s"])
                    ys.append(m.get("max_rss_mb") or 0)
            ax.scatter(xs, ys, marker=mk, label=f"{v['id']}-{track}" if xs else None,
                       color=plt.cm.viridis(i / max(len(versions) - 1, 1)), alpha=0.8)
    ax.set_xlabel("wall time (s)")
    ax.set_ylabel("peak RSS (MB)")
    ax.set_title("EM-Bench: resource usage per run")
    ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig3_resources.png")
    plt.close(fig)

    # fig4: 综合分
    fig, ax = plt.subplots(figsize=(7, 4))
    prim = [c for c in comp_rows]
    xs = list(range(len(prim)))
    parts = ["hit10", "ndcg10", "best_grade10", "ef10", "prec10", "rho"]
    bottoms = [0.0] * len(prim)
    cols = ["#1b9e77", "#d95f02", "#7570b3", "#e7298a", "#66a61e", "#e6ab02"]
    for pi, p in enumerate(parts):
        vals = [c[p] for c in prim]
        ax.bar(xs, vals, bottom=bottoms, label=p, color=cols[pi], width=0.6)
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    ax.set_xticks(xs)
    ax.set_xticklabels([c["version"] for c in prim])
    ax.set_ylabel("composite score (100)")
    ax.set_title("EM-Bench: composite dashboard score by version")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig4_composite.png")
    plt.close(fig)

    # fig5: 一致性热图（版本 × 版本 平均 Jaccard）
    vids = [v["id"] for v in versions]
    n = len(vids)
    mat = [[0.0] * n for _ in range(n)]
    for c in consistency:
        i, j = vids.index(c["v1"]), vids.index(c["v2"])
        mat[i][j] = mat[j][i] = c["jaccard"]
    for i in range(n):
        mat[i][i] = 1.0
    fig, ax = plt.subplots(figsize=(5.5, 4.5))
    im = ax.imshow(mat, cmap="YlGnBu", vmin=0, vmax=1)
    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    ax.set_xticklabels(vids, rotation=45, ha="right", fontsize=8)
    ax.set_yticklabels(vids, fontsize=8)
    for i in range(n):
        for j in range(n):
            ax.text(j, i, f"{mat[i][j]:.2f}", ha="center", va="center", fontsize=7)
    ax.set_title("top-10 overlap: mean pairwise Jaccard by version")
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig5_consistency.png")
    plt.close(fig)
    print(f"图 -> {fig_dir}")


if __name__ == "__main__":
    sys.exit(main())
