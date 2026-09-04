#!/usr/bin/env python3
"""EM-Bench 版本管理（versions.json 操作 + 本地清理）。

子命令:
  list                 列出活跃版本（含 worktree / results 状态）；--all 也列归档
  add <commit> <id> <label> [--ref r] [--date d] [--features f] [--no-etk] [--reflect]
                       追加新版本条目（环境准备由 add_version.sh 完成）
  remove <id> [--reason r] [--purge] [--clean-worktree] [--clean-results] [--yes]
                       默认: 软移除（移入 excluded_archived，保留 worktree 与结果，
                       该版本退出后续评测，历史快照中的指标不受影响）
                       --purge: 从 JSON 彻底删除条目
                       --clean-worktree: 同时 git worktree remove（版本 worktree 可能
                       脏（如 preflight 修复过符号链接），使用 --force 并先打印）
                       --clean-results: 同时删除 results/<id>/（需 --yes 或交互确认）
  restore <id> [--has-etk] [--reflect] [--label l] [--date d]
                       从 excluded_archived 恢复为活跃版本
  sync [--commits N]    git fetch origin --prune 同步 GitHub 远端引用，汇总新提交
                       （只更新本地 clone，不修改 versions.json；纳入评测仍需 add）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VJSON = ROOT / "code" / "versions.json"


def load():
    return json.load(open(VJSON))


def save(data):
    json.dump(data, open(VJSON, "w"), indent=2, ensure_ascii=False)
    json.dumps(data)  # 校验可序列化


def results_stats(vid):
    ok = fail = 0
    rd = ROOT / "results" / vid
    if rd.is_dir():
        for f in sorted(rd.glob("t*/result.json")):
            try:
                r = json.load(open(f))
                ok += 1 if r.get("pipeline_success") else 0
                fail += 0 if r.get("pipeline_success") else 1
            except (json.JSONDecodeError, OSError):
                fail += 1
    return ok, fail


def confirm(prompt):
    if sys.stdin.isatty():
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    return False


def cmd_list(args):
    data = load()
    print("== 活跃版本 ==")
    for v in data["versions"]:
        wt = ROOT / "versions" / v["id"]
        ok, fail = results_stats(v["id"])
        wts = "worktree ✓" if (wt / ".git").exists() else "worktree ✗"
        res = f"results {ok}✓/{fail}✗" if ok + fail else "results —"
        etk = "etk" if v.get("has_etk") else "-"
        print(f"  {v['id']:<12} {v.get('commit','?'):<10} {v.get('date','?'):<12} "
              f"[{etk}] {wts}, {res}  {v.get('label','')}")
    if args.all and data.get("excluded_archived"):
        print("== 归档/排除 ==")
        for v in data["excluded_archived"]:
            print(f"  {v['ref']:<28} {v.get('commit','?'):<10} {v.get('reason','')}")
    return 0


def cmd_add(args):
    data = load()
    ids = [v["id"] for v in data["versions"]]
    arch = [v.get("commit") for v in data.get("excluded_archived", [])]
    if args.id in ids:
        print(f"!! 版本 id 已存在: {args.id}")
        return 1
    repo = data["repo"]
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                        args.commit + "^{commit}"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        print(f"!! commit 不存在于仓库 {repo}: {args.commit}")
        return 1
    if args.commit in arch:
        print(f"!! commit {args.commit} 在归档列表中（excluded_archived）——"
              "如确要重新评测请先从归档移除该条记录")
        return 1
    data["versions"].append({
        "id": args.id, "ref": args.ref, "commit": args.commit,
        "date": args.date, "label": args.label,
        "features": args.features or "",
        "has_etk": not args.no_etk, "has_reflect": args.reflect,
    })
    save(data)
    print(f"versions.json 已追加 {args.id} ({args.commit}, ref={args.ref})")
    print(f"ADDED_ID {args.id}")
    return 0


def cmd_remove(args):
    data = load()
    versions = data["versions"]
    idx = next((i for i, v in enumerate(versions) if v["id"] == args.id), None)
    if idx is None:
        print(f"!! 活跃版本中不存在: {args.id}")
        return 1
    v = versions[idx]
    if args.purge:
        del versions[idx]
    else:
        arch = data.setdefault("excluded_archived", [])
        arch.append({"ref": v.get("ref", ""), "commit": v.get("commit", ""),
                     "reason": args.reason or "用户移除（软移除，worktree/结果保留）"})
        del versions[idx]
    save(data)

    if args.clean_worktree:
        wt = ROOT / "versions" / args.id
        if (wt / ".git").exists():
            repo = data["repo"]
            print(f"git worktree remove --force {wt}")
            r = subprocess.run(["git", "-C", repo, "worktree", "remove", "--force",
                                str(wt)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
            if r.returncode != 0:
                print(f"  !! worktree 移除失败: {r.stderr.strip()[:300]}")
            else:
                print(f"  worktree 已移除: {wt}")
        else:
            print(f"  (无 worktree 可移除)")

    if args.clean_results:
        rd = ROOT / "results" / args.id
        if rd.is_dir():
            ok, fail = results_stats(args.id)
            print(f"将删除 results/{args.id}/（{ok} 成功 + {fail} 失败的结果）")
            if args.yes or confirm("确认删除结果数据?"):
                shutil.rmtree(rd)
                print(f"  已删除: {rd}")
            else:
                print("  未删除（未确认）。")
        else:
            print("  (无结果数据可删除)")

    print(f"已移除 {args.id}（{'彻底删除' if args.purge else '移入归档'}）。"
          "历史快照 history/ 不受影响。")
    return 0


def cmd_restore(args):
    data = load()
    arch = data.get("excluded_archived", [])
    idx = next((i for i, v in enumerate(arch) if v.get("ref") == args.id
                or v.get("commit") == args.id), None)
    if idx is None:
        print(f"!! 归档中不存在: {args.id}（按 ref 或 commit 匹配）")
        return 1
    v = arch[idx]
    del arch[idx]
    if getattr(args, "new_id", ""):
        new_id = args.new_id
    elif args.id not in (v.get("ref"), v.get("commit")):
        new_id = args.id
    else:
        new_id = args.id.replace("/", "_")
    data["versions"].append({
        "id": new_id,
        "ref": v.get("ref", ""), "commit": v.get("commit", ""),
        "date": args.date or "restored", "label": args.label or f"恢复自归档（原: {v.get('reason','')}）",
        "features": "", "has_etk": args.has_etk, "has_reflect": args.reflect,
    })
    save(data)
    print(f"已恢复 {args.id} -> 活跃版本 {new_id}（commit {v.get('commit')}）。"
          "若 has_etk/has_reflect 与实际不符，请用 --has-etk/--reflect 重试或直接编辑 versions.json。")
    print(f"RESTORED_ID {new_id}")
    return 0


def cmd_sync(args):
    """git fetch origin --prune 同步 GitHub 远端引用并汇总新提交。

    只更新本地 clone（versions.json 中的版本不受影响）；把新提交纳入 benchmark
    仍需 add 子命令。远端无变化 / fetch 失败都有明确输出，退出码 0/1。
    """
    data = load()
    repo = data["repo"]

    def remote_refs():
        out = {}
        r = subprocess.run(["git", "-C", repo, "for-each-ref",
                            "--format=%(refname) %(objectname)",
                            "refs/remotes/origin"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    out[parts[0]] = parts[1]
        return out

    def short(ref):
        return ref.replace("refs/remotes/", "")

    def list_new(old, new, n):
        rng = f"{old}..{new}" if old else new
        r = subprocess.run(["git", "-C", repo, "log", "--oneline", "--no-decorate",
                            "-n", str(n), rng],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        if r.returncode != 0:
            # 远端可能 force-push 导致 old 不可达 → 退回只列 new 的提交
            r = subprocess.run(["git", "-C", repo, "log", "--oneline", "--no-decorate",
                                "-n", str(n), new],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True)
        return r.stdout.strip().splitlines()

    def count_new(old, new):
        r = subprocess.run(["git", "-C", repo, "rev-list", "--count", f"{old}..{new}"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        return r.stdout.strip() if r.returncode == 0 else "?"

    before = remote_refs()
    print(f"git fetch origin --prune   （{repo}）")
    try:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"  # 非交互下不卡在账号密码提示
        r = subprocess.run(["git", "-C", repo, "fetch", "origin", "--prune"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        print("!! git fetch 超时（300s）。请检查网络后重试。")
        return 1
    if r.returncode != 0:
        print("!! git fetch 失败（网络/远端不可用）:")
        print(r.stdout.strip()[-2000:])
        return 1
    after = remote_refs()

    moved, added, deleted = [], [], []
    for ref in sorted(after):
        if ref not in before:
            added.append((ref, after[ref]))
        elif before[ref] != after[ref]:
            moved.append((ref, before[ref], after[ref]))
    deleted = [ref for ref in sorted(before) if ref not in after]

    if not (moved or added or deleted):
        print("同步完成：远端无新提交（所有 origin/* 引用均未变化）。")
        print("提示: benchmark 的版本不受影响——新提交需用 add 命令纳入评测。")
        return 0

    print("同步完成，变化如下：")
    for ref, old, new in moved:
        print(f"  {short(ref)}  {old[:8]}..{new[:8]}  +{count_new(old, new)} 提交:")
        for line in list_new(old, new, args.commits):
            print(f"      {line}")
    for ref, sha in added:
        print(f"  + 新远端引用 {short(ref)} @ {sha[:8]}（前 {args.commits} 条提交）:")
        for line in list_new("", sha, args.commits):
            print(f"      {line}")
    for ref in deleted:
        print(f"  - 远端引用已删除: {short(ref)}（--prune）")
    print("提示: 同步只更新本地 clone 的远端引用；把新提交纳入 benchmark:")
    print("  bash manage_versions.sh add <commit> <版本id> <说明>")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list")
    p_list.add_argument("--all", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_add = sub.add_parser("add")
    p_add.add_argument("commit")
    p_add.add_argument("id")
    p_add.add_argument("label")
    p_add.add_argument("--ref", default="main")
    p_add.add_argument("--date", default="")
    p_add.add_argument("--features", default="")
    p_add.add_argument("--no-etk", action="store_true")
    p_add.add_argument("--reflect", action="store_true")
    p_add.set_defaults(func=cmd_add)

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("id")
    p_rm.add_argument("--reason", default="")
    p_rm.add_argument("--purge", action="store_true")
    p_rm.add_argument("--clean-worktree", action="store_true")
    p_rm.add_argument("--clean-results", action="store_true")
    p_rm.add_argument("--yes", action="store_true")
    p_rm.set_defaults(func=cmd_remove)

    p_rs = sub.add_parser("restore")
    p_rs.add_argument("id", help="匹配键：归档条目的 ref 或 commit")
    p_rs.add_argument("--new-id", default="", help="恢复后的版本 id（默认由匹配键推导）")
    p_rs.add_argument("--has-etk", action="store_true")
    p_rs.add_argument("--reflect", action="store_true")
    p_rs.add_argument("--label", default="")
    p_rs.add_argument("--date", default="")
    p_rs.set_defaults(func=cmd_restore)

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--commits", type=int, default=8,
                        help="每个更新分支展示的新提交条数（默认 8）")
    p_sync.set_defaults(func=cmd_sync)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
