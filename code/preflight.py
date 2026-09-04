#!/usr/bin/env python3
"""EM-Bench 运行前自检 + 自动修复（pre-flight）。

把首次基准运行的排障经验固化为自动检查，防止环境变化后旧坑复发：

 1. 符号链接陷阱（v1 全败根因）：EnzymeCAGE 管线 5 文件的符号链接会导致
    (a) Path(__file__).resolve() 穿透 worktree → 引用原始安装的悬空权重；
    (b) CPython 对符号链接主脚本把 sys.path[0] 解析到真实路径 → import 错位。
    修复方式与首轮一致：替换为真实文件（--fix，默认开启；--no-fix 只报告）。
 2. rxnmapper 权重可读性：pytorch_model.bin / training_args.bin 目标必须存活。
 3. enzymecage.dataset 可导入性（用 enzymecage 解释器轻量探测，无需 GPU）。
 4. 资源存在性：CAGE checkpoint、p2rank、HF 缓存（ESM-C 模型）、.env（只查存在，不读内容）。
 5. 运行环境：enzymecage python、miniprot env、java（p2rank 需要）。

用法:  python preflight.py [--fix|--no-fix] [--versions v1_main,v2_etk] [--out preflight.json]
退出码: 0 = 全部通过（含已自动修复）；1 = 存在未修复问题（阻塞运行）。
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 管线实际执行、且必须为真实文件（首轮修复清单）
PIPELINE_FILES = [
    "tools_src/EnzymeCAGE/scripts/run_mining_pipeline.py",
    "tools_src/EnzymeCAGE/scripts/prepare_mining_input.py",
    "tools_src/EnzymeCAGE/scripts/extract_p2rank_pockets.py",
    "tools_src/EnzymeCAGE/scripts/mining_utils.py",
    "tools_src/EnzymeCAGE/infer.py",
    "tools_src/EnzymeCAGE/feature/main.py",
]
RXNR_MODEL = ("tools_src/EnzymeCAGE/feature/pkgs/rxnmapper/models/transformers/"
              "albert_heads_8_uspto_all_1310k")
CHECKPOINT = "tools_src/EnzymeCAGE/checkpoints/pretrain/seed_42/epoch_19.pth"
P2RANK = "tools_src/p2rank/p2rank_2.5.1"
ENZYMECAGE_PY = "/data/gulab/igem2026/data11/igem_software/envs/enzymecage/bin/python"
JAVA_HOME = "/data/gulab/igem2026/data11/igem_software/envs/enzymecage"
MINIPROT_ENV = "/data/gulab/igem2026/data11/igem_software/envs/miniprot"
HF_CACHE = ROOT / "assets" / "hf_cache"
ESMC_DIR = HF_CACHE / "hub" / "models--EvolutionaryScale--esmc-600m-2024-12"

ISSUES = []   # (version, severity, message)
FIXES = []    # (version, message)


def issue(version, severity, msg):
    ISSUES.append({"version": version, "severity": severity, "message": msg})
    print(f"  [{severity}] {version}: {msg}")


def fixed(version, msg):
    FIXES.append({"version": version, "message": msg})
    print(f"  [fixed] {version}: {msg}")


def is_symlink(p: Path) -> bool:
    try:
        return p.is_symlink()
    except OSError:
        return False


def deref_exists(p: Path) -> bool:
    """符号链接目标是否存活（可 stat）。"""
    try:
        p.stat()
        return True
    except OSError:
        return False


def check_symlink_trap(version: dict, worktree: Path, fix: bool):
    vid = version["id"]
    for rel in PIPELINE_FILES:
        p = worktree / rel
        if is_symlink(p):
            if fix:
                target = os.readlink(p)
                if not os.path.isabs(target):
                    target = str((p.parent / target).resolve())
                # 链接目标可能是另一个链接；用真实内容复制
                try:
                    data = Path(target).read_bytes()
                except OSError as e:
                    issue(vid, "fatal", f"{rel} 是符号链接且目标不可读: {e}")
                    continue
                p.unlink()
                p.write_bytes(data)
                fixed(vid, f"{rel} 符号链接已替换为真实文件")
            else:
                issue(vid, "fatal", f"{rel} 是符号链接（会触发 ROOT_DIR/sys.path 穿透）; "
                                    "用 --fix 自动替换为真实文件")


def check_rxnr_weights(version: dict, worktree: Path):
    vid = version["id"]
    mdir = worktree / RXNR_MODEL
    if not mdir.is_dir():
        issue(vid, "fatal", f"rxnmapper 模型目录缺失: {RXNR_MODEL}")
        return
    for fname in ("pytorch_model.bin", "training_args.bin", "config.json", "vocab.txt"):
        p = mdir / fname
        if is_symlink(p) and not deref_exists(p):
            issue(vid, "fatal", f"rxnmapper 悬空链接: {RXNR_MODEL}/{fname} -> {os.readlink(p)}")
        elif not is_symlink(p) and not p.exists():
            issue(vid, "fatal", f"rxnmapper 缺文件: {RXNR_MODEL}/{fname}")


def check_dataset_import(version: dict, worktree: Path):
    vid = version["id"]
    cage = worktree / "tools_src" / "EnzymeCAGE"
    if not cage.is_dir():
        issue(vid, "fatal", "tools_src/EnzymeCAGE 目录缺失")
        return
    probe = (
        "import sys; sys.path.insert(0, '.'); "
        "import enzymecage, enzymecage.dataset; "
        "from enzymecage.dataset.geometric import load_geometric_dataset; "
        "print('OK')"
    )
    r = subprocess.run([ENZYMECAGE_PY, "-c", probe], cwd=cage,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=120)
    if r.returncode != 0 or "OK" not in r.stdout:
        issue(vid, "fatal", f"enzymecage.dataset 导入失败: {r.stderr.strip()[:300]}")


def check_assets(version: dict, worktree: Path):
    vid = version["id"]
    for rel, what in ((CHECKPOINT, "CAGE checkpoint"), (P2RANK, "p2rank 目录")):
        p = worktree / rel
        if is_symlink(p) and not deref_exists(p):
            issue(vid, "fatal", f"{what} 悬空链接: {rel} -> {os.readlink(p)}")
        elif not is_symlink(p) and not p.exists():
            issue(vid, "fatal", f"{what} 缺失: {rel}")
    env_file = worktree / ".env"
    if not env_file.exists():
        issue(vid, "warning", ".env 不存在（cage 工具链可能缺配置路径）")


def check_global_env():
    if not Path(ENZYMECAGE_PY).exists():
        issue("global", "fatal", f"enzymecage python 缺失: {ENZYMECAGE_PY}")
    if not Path(MINIPROT_ENV).is_dir():
        issue("global", "fatal", f"miniprot 环境缺失: {MINIPROT_ENV}")
    if not Path(JAVA_HOME, "bin", "java").exists():
        issue("global", "fatal", f"JAVA_HOME 无 java: {JAVA_HOME}")
    if not ESMC_DIR.is_dir():
        issue("global", "fatal", f"共享 HF 缓存缺 ESM-C 模型: {ESMC_DIR}")
    if not (ROOT / "code" / "env.sh").exists():
        issue("global", "fatal", "code/env.sh 缺失")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", dest="fix", action="store_true", default=True)
    ap.add_argument("--no-fix", dest="fix", action="store_false")
    ap.add_argument("--versions", default="", help="逗号分隔版本 id；默认全部")
    ap.add_argument("--out", default=str(ROOT / "results" / "preflight.json"))
    args = ap.parse_args()

    versions = json.load(open(ROOT / "code" / "versions.json"))["versions"]
    if args.versions:
        wanted = {v.strip() for v in args.versions.split(",") if v.strip()}
        versions = [v for v in versions if v["id"] in wanted]

    print(f"== EM-Bench preflight（{len(versions)} 个版本, fix={'on' if args.fix else 'off'}）")
    check_global_env()
    for v in versions:
        wt = ROOT / "versions" / v["id"]
        if not wt.is_dir():
            issue(v["id"], "fatal", f"worktree 缺失: {wt}")
            continue
        print(f"-- {v['id']}")
        check_symlink_trap(v, wt, args.fix)
        check_rxnr_weights(v, wt)
        check_dataset_import(v, wt)
        check_assets(v, wt)

    fatal = [i for i in ISSUES if i["severity"] == "fatal"]
    out = {"fixes": FIXES, "issues": ISSUES, "fatal": len(fatal),
           "warnings": sum(1 for i in ISSUES if i["severity"] == "warning")}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\npreflight: {len(fatal)} fatal, {out['warnings']} warning, {len(FIXES)} fixed -> {args.out}")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
