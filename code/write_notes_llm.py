#!/usr/bin/env python3
"""EM-Bench LLM 报告撰写器（可选步骤，不依赖 GPU/conda，系统 python3 即可）。

把 results/ 压缩为**数据摘要**（digest），调用 LLM API 生成评价解读。
默认写到 results/report/evaluation_notes_llm.md，**绝不覆盖**手工维护的
docs/evaluation_notes.md。

凭据全部来自环境变量（不落盘、不打印，错误信息中自动掩码 key）：
    EMBENCH_LLM_BASE_URL   OpenAI 兼容或 Anthropic 端点（必填）
    EMBENCH_LLM_API_KEY    API key（必填）
    EMBENCH_LLM_MODEL      模型名（必填；如 deepseek-chat / moonshot-v1 / claude-sonnet-5）
    EMBENCH_LLM_MAX_TOKENS 输出上限（默认 4096）
    EMBENCH_LLM_TEMPERATURE（默认 0.3）
Anthropic 原生协议与 OpenAI 兼容协议按 base URL 自动识别
（含 "anthropic" 走 Anthropic 协议，其余按 OpenAI 兼容处理）。

用法:
    python code/write_notes_llm.py                  # 读 results/，调 API 生成 notes
    python code/write_notes_llm.py --dry-run        # 只生成 prompt（落盘 llm_prompt.md），不调 API
    python code/write_notes_llm.py --dump-digest    # 只导出摘要 llm_digest.md（给外部 LLM/人工用）
    python code/write_notes_llm.py --out 路径.md    # 自定义输出路径
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 提示模板版本：改动生成逻辑时 +1，写入生成文件头供追溯
PROMPT_TEMPLATE_VERSION = "1"

SYSTEM_PROMPT = (
    "你是 EM-Bench（enzyme_update 多版本酶挖掘 benchmark）的评估分析师。"
    "用户会给你一段自动生成的数据摘要（digest），请你撰写中文 Markdown 评价解读，"
    "输出结构遵循：\n"
    "## 1. 核心结果一览 —— 用表格总结版本 × 任务的关键指标（Hit@10/PoolRecall/nDCG/综合分）。\n"
    "## 2. 命中与候选通道实证 —— 逐任务说明锚点是否进池、来自哪个检索通道"
    "（ec/keyword/text/rhea）、排名；引用 candidate_source 证据。\n"
    "## 3. 未命中任务分层归因 —— 区分检索设计问题（CLAIRE EC 不准、reviewed_only、"
    "回退未触发）、数据问题（锚点已删除/不在 UniProtKB）、部署问题（步骤失败）。\n"
    "## 4. 版本进步结论 —— 以数字为依据比较版本；注意协议版本不同时的解读边界。\n"
    "## 5. 对 enzyme_update 的改进建议 —— 具体到检索链/参数。\n"
    "## 6. 局限 —— 本报告为 LLM 自动生成，请人工核对后再采信。\n\n"
    "硬性规则：\n"
    "1. 所有数字、accession、排名必须来自 digest，禁止编造；摘要里没有的写「摘要未提供」。\n"
    "2. 归因要引用 digest 中的证据字段（如 candidate_source、trEMBL_anchor_in_pool、"
    "step_failures），不得凭空推断。\n"
    "3. 结论句写清楚适用的版本与协议版本（benchmark_version）。\n"
    "4. 不要重复摘要全文；每节 3-8 行即可，总长控制在 100 行内。\n"
)


def mask(key):
    if not key:
        return "(未设置)"
    if len(key) <= 8:
        return "***"
    return key[:4] + "***" + key[-2:]


# ------------------------------------------------- 与 enzyme_update 共用 API 配置
PROVIDER_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "deepseek-v3": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "openai-mini": "https://api.openai.com/v1",
}


def parse_env_file(path):
    """极简 .env 解析：KEY=VALUE / export KEY=VALUE，忽略注释与空行。"""
    out = {}
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_dotenv_file(path):
    """把 .env 读入 os.environ（已存在的环境变量优先，等价 python-dotenv 默认语义）。
    返回本次新加入的键数。"""
    added = 0
    for k, v in parse_env_file(path).items():
        if k not in os.environ:
            os.environ[k] = v
            added += 1
    return added


def parse_yaml_llm(path):
    """读取 config/settings.yaml 的 llm: 段（只取 provider/api_key/model/base_url）。

    优先 PyYAML；不可用时做严格的行级子集解析（仅顶层 llm: 段）。"""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(text) or {}
        llm = cfg.get("llm") or {}
        return {k: llm.get(k) for k in ("provider", "api_key", "model", "base_url")}
    except Exception:
        pass
    out = {}
    in_llm = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if re.match(r"^llm:\s*(#.*)?$", line):
            in_llm = True
            continue
        if in_llm and re.match(r"^\S", line):  # 回到顶层键
            break
        if in_llm:
            m = re.match(r"^\s+(provider|api_key|model|base_url)\s*:\s*(.*?)\s*(?:#.*)?$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def resolve_llm_config(enzyme_repo=""):
    """解析 LLM 配置（复用 enzyme_update 的 API，与项目相同的 key/模型）。

    优先级（高→低）：
      1. EMBENCH_LLM_* 环境变量（显式覆盖）
      2. enzyme_update 仓库：config/settings.yaml(llm:) → .env(DEEPSEEK_*)
      3. DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL 环境变量
      4. 提供商预设（默认 deepseek）
    返回 (base_url, api_key, model, 来源说明)。只提取 4 个字段，绝不落盘、不打印明文。
    """
    bu = os.environ.get("EMBENCH_LLM_BASE_URL", "").strip()
    ak = os.environ.get("EMBENCH_LLM_API_KEY", "").strip()
    md = os.environ.get("EMBENCH_LLM_MODEL", "").strip()
    if bu or ak or md:
        return bu, ak, md, "EMBENCH_LLM_* 环境变量"

    settings, env_file = {}, {}
    repo = Path(enzyme_repo) if enzyme_repo else None
    if repo and repo.is_dir():
        sp = repo / "config" / "settings.yaml"
        if sp.is_file():
            settings = parse_yaml_llm(sp)
        ep = repo / ".env"
        if ep.is_file():
            env_file = parse_env_file(ep)

    provider = settings.get("provider") or "deepseek"
    api_key = (settings.get("api_key") or "").strip() or \
        env_file.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "") or \
        env_file.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    model = (settings.get("model") or "").strip() or \
        env_file.get("DEEPSEEK_MODEL") or os.environ.get("DEEPSEEK_MODEL", "") or "deepseek-chat"
    base_url = (settings.get("base_url") or "").strip() or \
        env_file.get("DEEPSEEK_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL", "") or \
        PROVIDER_BASE_URLS.get(provider, "")
    src = f"enzyme_update 配置（settings.yaml/.env @ {repo}）" if (repo and (settings or env_file)) \
        else "DEEPSEEK_* 环境变量 / 提供商预设"
    return base_url, api_key, model, src


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


# ------------------------------------------------------------------ digest
def build_digest(results_root, max_chars):
    """把 results/ 压缩成结构化文本摘要（LLM 输入）。"""
    L = []
    add = L.append
    tasks_doc = load_json(ROOT / "tasks" / "tasks.json", {})
    versions_doc = load_json(ROOT / "code" / "versions.json", {})
    tasks = tasks_doc.get("tasks") or []
    versions = versions_doc.get("versions") or []

    add(f"# EM-Bench 数据摘要（{now_iso()}）")
    add(f"- benchmark_version: {tasks_doc.get('benchmark_version', '?')}")
    add("- protocol_changelog: " + "; ".join(
        f"{c.get('version')}({c.get('date')}): {c.get('change', '')[:60]}"
        for c in tasks_doc.get("protocol_changelog", [])))
    add("- 版本集: " + "; ".join(
        f"{v['id']}@{v.get('commit', '?')} ({v.get('label', '')[:24]}, "
        f"search_space={v.get('search_space', '?')})" for v in versions))
    add("")

    # 任务与锚点（含可达性）
    anchors = load_json(ROOT / "tasks" / "anchors.json", {})
    add("## 任务与锚点")
    for t in tasks:
        accs = ", ".join(
            f"{a['accession']}(grade{a['grade']},"
            f"{'Swiss-Prot' if anchors.get(a['accession'], {}).get('reachable') else 'TrEMBL/不可达'})"
            for a in t.get("anchors", []))
        add(f"- {t['id']}: 难度 {t.get('difficulty', '?')}; goal_text="
            f"{(t.get('goal_text') or '')[:70]}; 锚点: {accs or '无'}")
    add("")

    # 逐运行结果（从 result.json，比 metrics.csv 更全：候选来源/失败步骤）
    import csv as _csv
    metrics = {}
    mp = results_root / "report" / "metrics.csv"
    if mp.exists():
        with open(mp, newline="") as f:
            for r in _csv.DictReader(f):
                metrics[(r["task_id"], r["version_id"], r["track"])] = r
    add("## 逐任务 × 版本结果（primary=主流程 reaction_full, etk=enzyme-tk 轨道）")
    for t in tasks:
        add(f"### {t['id']}")
        for v in versions:
            for track, suffix in (("primary", ""), ("etk", "__etk")):
                if track == "etk" and not v.get("has_etk"):
                    continue
                rj = load_json(results_root / v["id"] / (t["id"] + suffix) / "result.json")
                row = metrics.get((t["id"], v["id"], track), {})
                if rj is None:
                    add(f"  {v['id']}/{track}: 未运行")
                    continue
                parts = [f"{v['id']}/{track}",
                         f"success={rj.get('pipeline_success')}",
                         f"fail={rj.get('failure_type') or '-'}",
                         f"n_cand={rj.get('n_candidates')}",
                         f"hit10_all={row.get('hit10_all', '?')}",
                         f"best_rank={row.get('best_rank', '?')}",
                         f"pool_recall_all={row.get('pool_recall_all', '?')}",
                         f"ndcg={row.get('ndcg10', '?')}",
                         f"trEMBL_in_pool={row.get('trEMBL_anchor_in_pool') or '-'}"]
                # 候选来源分布（读 session 产物，仅在有值/非纯 ec 时给出）
                sess = load_json(results_root / v["id"] / (t["id"] + suffix)
                                 / "artifacts" / "session.json")
                if sess:
                    cs = Counter((sess.get("candidate_source") or {}).values())
                    if cs and set(cs) != {"ec"}:
                        parts.append("candidate_source=" + ",".join(
                            f"{k}:{n}" for k, n in sorted(cs.items())))
                    n_text = len(sess.get("text_ids") or [])
                    n_rhea = len(sess.get("rhea_ids") or [])
                    if n_text or n_rhea:
                        parts.append(f"text_ids={n_text} rhea_ids={n_rhea}")
                sf = rj.get("step_failures") or []
                if sf:
                    parts.append(f"step_failures={','.join(sf)}")
                add("  " + " | ".join(parts))
        add("")

    # 汇总（metrics.csv 版本 × 轨道行）
    add("## 版本 × 轨道汇总")
    for v in versions:
        for track in ("primary", "etk"):
            if track == "etk" and not v.get("has_etk"):
                continue
            agg = [f"{v['id']}/{track}"]
            for col in ("hit10_all", "ndcg10", "pool_recall_all", "prec10"):
                vals = [float(m[col]) for (tid, vid, tr), m in metrics.items()
                        if vid == v["id"] and tr == track and m.get(col) not in (None, "", "None")]
                agg.append(f"{col}={sum(vals) / len(vals):.3f}" if vals else f"{col}=n/a")
            add("  " + " | ".join(agg))
    add("")

    # cage 审计（版本间特征一致性证据）
    cage = load_json(results_root / "report" / "cage_audit.csv")
    if cage is None and (results_root / "report" / "cage_audit.csv").exists():
        add("## CAGE 特征审计")
        for line in (results_root / "report" / "cage_audit.csv").read_text(
                encoding="utf-8").splitlines()[:12]:
            add("  " + line)
        add("")

    digest = "\n".join(L)
    if len(digest) > max_chars:
        digest = digest[:max_chars] + "\n...（摘要超长已截断，请基于以上数据撰写）"
    return digest


# ------------------------------------------------------------------ LLM
def call_anthropic(base_url, api_key, model, max_tokens, temperature, prompt):
    url = base_url.rstrip("/") + "/v1/messages"
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("content") or [{}])[0].get("text", "")


def call_openai_compat(base_url, api_key, model, max_tokens, temperature, prompt):
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


def _default_enzyme_repo():
    try:
        return json.load(open(ROOT / "code" / "versions.json")).get("repo", "")
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "results"))
    ap.add_argument("--out", default="")
    ap.add_argument("--dry-run", action="store_true", help="只生成 prompt，不调 API")
    ap.add_argument("--dump-digest", action="store_true", help="只导出摘要，不调 API")
    ap.add_argument("--max-input-chars", type=int, default=24000)
    ap.add_argument("--enzyme-repo", default=_default_enzyme_repo(),
                    help="enzyme_update 仓库路径（读取其 config/settings.yaml 与 .env，"
                         "复用与项目相同的 LLM API；默认取 versions.json 的 repo 字段）")
    args = ap.parse_args()

    results_root = Path(args.results)
    digest = build_digest(results_root, args.max_input_chars)
    digest_path = results_root / "report" / "llm_digest.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(digest, encoding="utf-8")
    print(f"[llm_report] 摘要 -> {digest_path}（{len(digest)} 字符）")

    if args.dump_digest:
        print("[llm_report] --dump-digest 完成（未调 API）。")
        return 0

    prompt = (f"以下是一次 benchmark 运行的数据摘要，请按要求撰写评价解读"
              f"（输出纯 Markdown 正文，不要用代码块包裹）：\n\n{digest}")
    prompt_path = results_root / "report" / "llm_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    if args.dry_run:
        print(f"[llm_report] --dry-run：prompt -> {prompt_path}（未调 API）。")
        return 0

    # 自动加载本仓库 .env（类似 enzyme_update 的 .env；已存在的环境变量优先）
    dotenv_path = ROOT / ".env"
    if dotenv_path.is_file():
        n = load_dotenv_file(dotenv_path)
        if n:
            print(f"[llm_report] 已自动加载 {dotenv_path}（{n} 项；已存在的环境变量优先）")

    base_url, api_key, model, cfg_src = resolve_llm_config(args.enzyme_repo)
    max_tokens = int(os.environ.get("EMBENCH_LLM_MAX_TOKENS", "4096"))
    temperature = float(os.environ.get("EMBENCH_LLM_TEMPERATURE", "0.3"))
    if not (base_url and api_key and model):
        print("[llm_report] 无可用 LLM 凭据：跳过 API 调用（摘要 llm_digest.md 已生成，"
              "可离线交给任何 LLM）。配置方式：本仓库 .env（模板 .env.example）"
              "或 EMBENCH_LLM_* 环境变量，或本机 enzyme_update 的 .env / settings.yaml"
              "（与项目共用同一 API）。")
        return 0

    print(f"[llm_report] 调用 {model} @ {base_url}（key={mask(api_key)}，"
          f"max_tokens={max_tokens}，配置来源: {cfg_src}）...")
    t0 = time.time()
    try:
        if "anthropic" in base_url.lower():
            text = call_anthropic(base_url, api_key, model, max_tokens, temperature, prompt)
        else:
            text = call_openai_compat(base_url, api_key, model, max_tokens, temperature, prompt)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print(f"[llm_report] !! API 错误 HTTP {e.code}: {detail}")
        return 1
    except Exception as e:
        print(f"[llm_report] !! 请求失败（{type(e).__name__}: {e}；未打印 key）。")
        return 1

    if not text or not text.strip():
        print("[llm_report] !! 模型返回空文本。")
        return 1

    header = (
        "# EM-Bench 评价解读（LLM 自动生成）\n\n"
        f"> 生成时间: {now_iso()}\n"
        f"> 模型: {model} @ {base_url}\n"
        f"> 数据: {results_root}（摘要 {len(digest)} 字符，见 llm_digest.md）\n"
        f"> 提示模板版本: {PROMPT_TEMPLATE_VERSION}；耗时 {time.time() - t0:.1f}s\n"
        "> 本文件由 code/write_notes_llm.py 生成，**不覆盖**手工维护的 "
        "evaluation_notes.md；数字与归因请人工核对后采信。\n\n"
    )
    out = Path(args.out) if args.out else results_root / "report" / "evaluation_notes_llm.md"
    out.write_text(header + text.strip() + "\n", encoding="utf-8")
    print(f"[llm_report] 完成 -> {out}（{len(text)} 字符，{time.time() - t0:.1f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
