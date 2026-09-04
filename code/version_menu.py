#!/usr/bin/env python3
"""EM-Bench 版本管理交互菜单（终端窗口模式，零依赖，Python 3.6+）。

用法:  python3 version_menu.py     （或 bash manage_versions.sh menu / 无参数直接进）
主菜单: [1] 查看版本  [2] 添加版本  [3] 删除版本  [4] 恢复归档  [5] 一键运行基准
        [6] 同步 GitHub  [7] 最近提交(含时间)  [8] 全部版本(分支/标签)  [0] 退出
所有操作复用 manage_versions.py 的子命令逻辑（JSON 安全校验不变），
添加/恢复后自动完成 worktree 检出 + preflight 自检。
"""
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "code"))

from manage_versions import (cmd_add, cmd_list, cmd_remove, cmd_restore,  # noqa: E402
                             cmd_sync, load)

C = {"reset": "\033[0m", "bold": "\033[1m", "green": "\033[32m",
     "yellow": "\033[33m", "cyan": "\033[36m", "red": "\033[31m"}


def c(text, *styles):
    return "".join(C.get(s, "") for s in styles) + text + C["reset"]


def banner():
    print()
    print(c("  ┌────────────────────────────────────────────┐", "cyan"))
    print(c("  │        EM-Bench 版本管理（交互模式）       │", "cyan", "bold"))
    print(c("  └────────────────────────────────────────────┘", "cyan"))


def ask(prompt, default=None, choices=None):
    """循环询问直到合法输入。choices: (allowed_str, desc) 列表。"""
    while True:
        hint = f" [{default}]" if default else ""
        raw = input(f"  {prompt}{hint}: ").strip()
        if not raw and default:
            raw = default
        if not raw:
            print(c("  （输入不能为空）", "red"))
            continue
        if choices and raw.lower() not in [ch.lower() for ch, _ in choices]:
            print(c("  可选: " + " / ".join(f"{k}({d})" for k, d in choices), "yellow"))
            continue
        return raw


def ask_bool(prompt, default=False):
    return ask(prompt, "y" if default else "n", [("y", "是"), ("n", "否")]).lower() == "y"


def pick(items, title):
    """编号选择，返回选中项；空列表返回 None。"""
    if not items:
        print(c(f"  （{title} 为空）", "yellow"))
        return None
    print(c(f"  == {title} ==", "bold"))
    for i, it in enumerate(items, 1):
        print(f"  {i:>2}. {it}")
    while True:
        raw = input("  选择编号（回车返回）: ").strip()
        if not raw:
            return None
        try:
            n = int(raw)
        except ValueError:
            continue
        if 1 <= n <= len(items):
            return items[n - 1]
        print(c("  编号超出范围", "red"))


def recent_commits(n=8):
    data = load()
    # --all：跨所有分支/远端引用列出最近提交（sync 拉取的 origin/* 新提交
    # 不在当前 HEAD 历史里，仅用 git log 看不到）；--date=short 附提交时间
    r = subprocess.run(["git", "-C", data["repo"], "log", "--all", f"-{n}",
                        "--date=short", "--pretty=format:%h %ad %s"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       universal_newlines=True)
    if r.returncode == 0:
        print(c(f"  == {data['repo']} 最近提交（全部引用，含时间） ==", "bold"))
        for line in r.stdout.splitlines():
            print(f"    {line}")
    else:
        print(c("  （无法读取仓库最近提交）", "yellow"))


def run_env_steps(vid):
    """worktree + 资产 + preflight（与 manage_versions.sh add/restore 相同）。"""
    print(c("  == 检出 worktree + 资产软链（幂等） ==", "bold"))
    r = subprocess.run(["bash", str(ROOT / "code" / "setup_versions.sh")])
    if r.returncode != 0:
        print(c("  !! setup_versions.sh 失败", "red"))
        return False
    print(c("  == preflight 自检 ==", "bold"))
    r = subprocess.run(["bash", "-c", (
        f"source '{ROOT}/code/env.sh' 2>/dev/null; "
        f"python3 '{ROOT}/code/preflight.py' --versions '{vid}' "
        f"--out '{ROOT}/results/preflight.json'")])
    if r.returncode != 0:
        print(c("  !! preflight 存在 fatal 问题（results/preflight.json）", "red"))
        return False
    return True


def menu_add():
    print()
    print(c("  == 添加版本 ==", "bold"))
    recent_commits()
    commit = ask("commit（可输入上面列表的 hash 或编号；编号 1 = 最新）")
    if commit.isdigit():
        data = load()
        n = int(commit)
        # 与 recent_commits 一致用 --all；编号 N = 列表第 N 行（第 1 行最新）
        r = subprocess.run(["git", "-C", data["repo"], "log", "--all", f"-{n}",
                            "--format=%H"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True)
        if r.returncode == 0:
            lines = r.stdout.splitlines()
            if 1 <= n <= len(lines):
                commit = lines[n - 1].strip()
                print(c(f"  -> commit {commit[:8]}", "green"))
            else:
                print(c("  编号超出列表范围，请重新输入完整 hash 或重新添加。", "yellow"))
                return
    vid = ask("版本 id（如 v6_future）")
    label = ask("一句话说明（label）")
    ref = ask("ref（分支名，供记录）", "main")
    has_etk = ask_bool("该版本是否有 enzyme-tk 流程（etk 轨道）?", False)
    has_reflect = ask_bool("该版本是否有 reflect 步骤?", False)
    print()
    print(c(f"  确认: id={vid} commit={commit} label={label} ref={ref} "
            f"etk={has_etk} reflect={has_reflect}", "yellow"))
    if not ask_bool("执行添加?"):
        print("  已取消。")
        return
    args = Namespace(commit=commit, id=vid, label=label, ref=ref, date="",
                     features="", no_etk=not has_etk, reflect=has_reflect)
    if cmd_add(args) != 0:
        return
    if run_env_steps(vid):
        print(c(f"  完成！运行基准: bash run_all.sh --versions {vid}", "green"))


def menu_remove():
    print()
    data = load()
    versions = data["versions"]
    print(c("  == 删除版本 ==", "bold"))
    if not versions:
        print(c("  （没有活跃版本）", "yellow"))
        return
    items = [f"{v['id']:<12} {v.get('commit',''):<10} {v.get('label','')}" for v in versions]
    choice = pick(items, "选择要删除的版本")
    if choice is None:
        return
    vid = choice.split()[0]
    purge = ask_bool("彻底删除（默认软移除，可随时 restore）?", False)
    clean_wt = ask_bool("同时移除 worktree（git worktree remove）?", False)
    clean_res = ask_bool("同时删除 results 结果数据?", False)
    reason = ""
    if not purge:
        reason = ask("移除原因（记录在归档中）", "用户移除")
    print()
    print(c(f"  确认: 移除 {vid}（{'彻底删除' if purge else '软移除'}） "
            f"clean_worktree={clean_wt} clean_results={clean_res}", "yellow"))
    if not ask_bool("执行?"):
        print("  已取消。")
        return
    args = Namespace(id=vid, reason=reason, purge=purge, clean_worktree=clean_wt,
                     clean_results=clean_res, yes=True)
    cmd_remove(args)


def menu_restore():
    print()
    data = load()
    arch = data.get("excluded_archived", [])
    print(c("  == 恢复归档版本 ==", "bold"))
    items = [f"{a.get('commit','?'):<10} ref={a.get('ref','?'):<28} {a.get('reason','')}"
             for a in arch]
    choice = pick(items, "归档条目")
    if choice is None:
        return
    a = arch[items.index(choice)]
    vid = ask("恢复后的版本 id", a.get("ref", "").replace("/", "_"))
    has_etk = ask_bool("是否有 enzyme-tk 流程?", False)
    has_reflect = ask_bool("是否有 reflect 步骤?", False)
    print()
    print(c(f"  确认: 恢复 {a.get('commit')} -> {vid} etk={has_etk} reflect={has_reflect}",
            "yellow"))
    if not ask_bool("执行恢复?"):
        print("  已取消。")
        return
    args = Namespace(id=a.get("commit", ""), new_id=vid, has_etk=has_etk,
                     reflect=has_reflect, label="", date="")
    # cmd_restore 按 ref/commit 匹配归档，返回新的活跃 id
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_restore(args)
    print(buf.getvalue(), end="")
    if rc != 0:
        return
    if run_env_steps(vid):
        print(c(f"  完成！运行基准: bash run_all.sh --versions {vid}", "green"))


def menu_recent_commits():
    """只显示仓库最近提交（全部引用，含提交时间）。"""
    print()
    recent_commits(15)


def menu_all_versions():
    """显示仓库全部分支/标签及 tip 提交，标注是否已被 benchmark 评测/归档。"""
    print()
    print(c("  == 仓库全部分支/标签（[已评测]=活跃版本, [已归档]=曾评测） ==", "bold"))
    data = load()
    active = {v.get("commit", "") for v in data["versions"]}
    archived = {v.get("commit", "") for v in data.get("excluded_archived", [])}

    def in_set(s, sha):
        return any(full and (sha == full or sha.startswith(full)) for full in s)

    r = subprocess.run(["git", "-C", data["repo"], "for-each-ref",
                        "--sort=-committerdate",
                        "--format=%(refname:short)%09%(objectname)%09"
                        "%(committerdate:short)%09%(subject)"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       universal_newlines=True)
    if r.returncode != 0:
        print(c("  （无法读取仓库引用）", "yellow"))
        return
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        ref, sha, date, subj = parts
        if in_set(active, sha):
            tag = c("[已评测]", "green")
        elif in_set(archived, sha):
            tag = c("[已归档]", "yellow")
        else:
            tag = ""
        print(f"  {ref:<38} {sha[:8]} {date} {subj[:48]} {tag}")


def menu_sync():
    print()
    print(c("  == 同步 GitHub ==", "bold"))
    print(c("  git fetch origin --prune（更新本地 clone 的远端引用，不修改版本配置）", "yellow"))
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_sync(Namespace(commits=8))
    print(buf.getvalue(), end="")
    if rc != 0:
        print(c("  !! 同步失败（见上方输出）", "red"))
        return
    if ask_bool("是否立即添加一个新版本?"):
        menu_add()


def menu_run():
    print()
    print(c("  == 一键运行基准 ==", "bold"))
    print("  1. 全部版本（primary + etk 两轨道）")
    print("  2. 指定版本")
    print("  3. 只重跑失败任务（--retry-failed）")
    choice = ask("选择", "1", [("1", ""), ("2", ""), ("3", "")])
    cmd = ["bash", str(ROOT / "run_all.sh")]
    if choice == "2":
        data = load()
        ids = [v["id"] for v in data["versions"]]
        sel = pick(ids, "选择版本（可多选，逗号分隔）")
        if sel is None:
            return
        raw = ask("版本 id（逗号分隔多选）", sel)
        cmd += ["--versions", raw]
    elif choice == "3":
        cmd += ["--retry-failed"]
    print(c(f"  -> {' '.join(cmd)}", "green"))
    if not ask_bool("提交到集群?"):
        print("  已取消。")
        return
    subprocess.run(cmd)


def main():
    while True:
        banner()
        print("   [1] 查看版本          [2] 添加版本")
        print("   [3] 删除版本          [4] 恢复归档版本")
        print("   [5] 一键运行基准      [6] 同步 GitHub")
        print("   [7] 最近提交          [8] 全部版本(分支/标签)")
        print("   [0] 退出")
        print()
        try:
            raw = input(c("  请选择: ", "bold")).strip()
        except EOFError:  # 非交互 stdin（如管道/脚本误调）→ 干净退出
            print(c("  （无交互输入，退出）", "yellow"))
            return 1
        if raw == "0":
            print("  再见。")
            return 0
        if raw == "1":
            cmd_list(Namespace(all=True))
        elif raw == "2":
            menu_add()
        elif raw == "3":
            menu_remove()
        elif raw == "4":
            menu_restore()
        elif raw == "5":
            menu_run()
        elif raw == "6":
            menu_sync()
        elif raw == "7":
            menu_recent_commits()
        elif raw == "8":
            menu_all_versions()
        else:
            print(c("  无效选择", "red"))
        input(c("\n  按回车继续…", "cyan"))


if __name__ == "__main__":
    sys.exit(main())
