#!/usr/bin/env python3
"""把部署副本（data11/igem_software/enzyme_update）中未入库的大文件软链到 benchmark worktree。

规则：对参考副本中存在的路径 P（tools_src/*、models/*、根目录 Pfam-A.hmm*），
若 P 未被当前 worktree 的 git 跟踪（git ls-files 为空），则用软链指向参考副本。
已跟踪的文件（vendored 代码）保持检出版本不动。
"""
import argparse
import os
import subprocess
from pathlib import Path


def tracked_paths(worktree: Path) -> set:
    r = subprocess.run(
        ["git", "ls-files"], cwd=worktree, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, universal_newlines=True
    )
    return set(r.stdout.splitlines()) if r.returncode == 0 else set()


def link_dir(ref: Path, wt: Path, rel: str, tracked: set) -> None:
    """recursively: tracked files stay; untracked files/dirs become symlinks to ref"""
    src = ref / rel
    dst = wt / rel
    if not os.path.exists(str(src)):
        return  # 源不存在（含断链）：跳过，让下一个 source 补齐
    if dst.is_symlink() and not os.path.exists(str(dst)):
        dst.unlink()  # 断链目录：移除，递归重建（否则 mkdir 父路径报错）
    for child in sorted(src.iterdir(), key=lambda p: p.name):
        crel = os.path.join(rel, child.name)
        if child.is_symlink() and not os.path.exists(str(child)):
            continue  # 源本身就是断链
        if child.is_dir():
            if child.name in ("__pycache__", ".git"):
                continue
            link_dir(ref, wt, crel, tracked)
            continue
        if crel in tracked:
            continue  # vendored code: keep checkout copy
        dst_child = dst / child.name
        if dst_child.is_symlink():
            if os.path.exists(str(dst_child)):
                continue
            dst_child.unlink()  # 断链：移除后从本 source 重链
        dst_child.parent.mkdir(parents=True, exist_ok=True)
        if dst_child.exists():
            dst_child.unlink()
        try:
            dst_child.symlink_to(os.path.realpath(child))
            print(f"  ln {crel}")
        except OSError as exc:
            print(f"  !! ln {crel} failed: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref-copy", required=True)
    ap.add_argument("--worktree", required=True)
    args = ap.parse_args()

    ref = Path(args.ref_copy).resolve()
    wt = Path(args.worktree).resolve()
    if not wt.is_dir():
        print(f"  worktree 不存在: {wt}")
        return

    tracked = tracked_paths(wt)
    print(f"== assets for {wt.name} (tracked files: {len(tracked)})")

    # 根目录 Pfam 文件（PFAM_HMM_PATH=Pfam-A.hmm 相对解析到 worktree 根）
    for p in sorted(ref.glob("Pfam-A.hmm*")):
        if not os.path.exists(str(p)):
            continue
        rel = p.name
        dst = wt / rel
        if rel in tracked:
            continue
        if dst.is_symlink():
            if os.path.exists(str(dst)):
                continue
            dst.unlink()
        if dst.exists():
            dst.unlink()
        try:
            dst.symlink_to(p)
            print(f"  ln {rel}")
        except OSError as exc:
            print(f"  !! ln {rel} failed: {exc}")

    # tools_src / models 逐文件处理（未跟踪的权重/模型软链，跟踪的代码保留）
    for top in ("tools_src", "models"):
        if (ref / top).is_dir():
            link_dir(ref, wt, top, tracked)


if __name__ == "__main__":
    main()
