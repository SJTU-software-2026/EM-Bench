#!/usr/bin/env bash
# EM-Bench 版本环境准备：
#   1) 用 git worktree 从 data10/enzyme_update 检出每个 benchmark 版本
#   2) 复制部署环境（data11/igem_software/enzyme_update）的 .env 与 settings.yaml
#   3) 将未入库的大文件（CLAIRE/CAGE 权重、P2Rank、Pfam、模型）软链到每个 worktree
#
# 依赖：code/link_assets.py、code/versions.json
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="/data/gulab/igem2026/data10/enzyme_update"
REF_COPY="/data/gulab/igem2026/data11/igem_software/enzyme_update"
VERSIONS_DIR="$ROOT/versions"

mkdir -p "$VERSIONS_DIR"
python3 - "$REPO" "$REF_COPY" "$VERSIONS_DIR" "$ROOT/code/versions.json" <<'PYEOF'
import json, os, subprocess, sys, shutil
from pathlib import Path

repo, ref_copy, versions_dir, versions_json = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
versions = json.load(open(versions_json))

def sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode != 0:
        print(f"  !! {cmd}\n  {r.stderr.strip()[:400]}")
        return False
    return True

for v in versions["versions"]:
    vid = v["id"]
    wt = Path(versions_dir) / vid
    print(f"== {vid} ({v['ref']} @ {v['commit']})")
    if not (wt / ".git").exists():
        # 直接按 commit 检出（benchmark 版本 = 具体提交，不依赖远端分支引用存活；
        # 若 commit 对象不在本地，fallback 到 ref 并 git fetch）
        ok = sh(f"git worktree add --detach '{wt}' '{v['commit']}'", cwd=repo)
        if not ok:
            sh("git fetch origin", cwd=repo)
            ok = sh(f"git worktree add --detach '{wt}' '{v['commit']}'", cwd=repo)
        if not ok:
            print(f"  SKIP {vid}: worktree 创建失败")
            continue
    else:
        print("  worktree 已存在，跳过检出")
    # 部署配置：.env 全为相对路径（tools_src/...），随 worktree 解释
    for f in (".env",):
        src = Path(ref_copy) / f
        if src.exists():
            shutil.copy2(src, wt / f)
            print(f"  copied {f}")
    # settings.yaml（可能未入库，但 CLI 固定流程路径通常不需要；复制以便兼容）
    ssrc = Path(ref_copy) / "config" / "settings.yaml"
    if ssrc.exists():
        (wt / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ssrc, wt / "config" / "settings.yaml")
        print("  copied config/settings.yaml")
print("\n== 大文件软链 ==")
sys.exit(0)
PYEOF

# 逐个 worktree 补软链（python 脚本避免 bash 转义地狱）。
# 顺序：部署副本（Pfam、通用资产）→ 主仓库（p2rank/CAGE/rxnmapper 等未入库资产）
#       → assets shim（CLAIRE zenodo data + GitHub model，EM-Bench 自备）
MAIN_REPO="/data/gulab/igem2026/data10/enzyme_update"
ASSETS_SHIM="$ROOT/assets/claire_shim"   # 内部按 worktree 布局镜像
for v in $(python3 -c "import json;print(' '.join(x['id'] for x in json.load(open('$ROOT/code/versions.json'))['versions']))"); do
    python3 "$ROOT/code/link_assets.py" --ref-copy "$REF_COPY" --worktree "$VERSIONS_DIR/$v"
    python3 "$ROOT/code/link_assets.py" --ref-copy "$MAIN_REPO" --worktree "$VERSIONS_DIR/$v"
    if [ -d "$ASSETS_SHIM" ]; then
        python3 "$ROOT/code/link_assets.py" --ref-copy "$ASSETS_SHIM" --worktree "$VERSIONS_DIR/$v"
    fi
done
echo "== etk 反应库（git-lfs 对象本地物化，无 git-lfs 二进制时直接从对象库拷贝） =="
LFS_OBJ="/data/gulab/igem2026/data11/igem_software/enzyme_update/.git/lfs/objects/d6/c9a8/d6c9a87ec4fef4cf8d5b6147b11301410ecde13971003cac62c38b9ec9090db7"
for v in $(python3 -c "import json;print(' '.join(x['id'] for x in json.load(open('$ROOT/code/versions.json'))['versions']))"); do
    WT="$VERSIONS_DIR/$v"
    if [ -f "$LFS_OBJ" ] && head -c 8 "$WT/etk_reaction_db.csv" 2>/dev/null | grep -q "version "; then
        cp "$LFS_OBJ" "$WT/etk_reaction_db.csv"
        echo "  $v: etk_reaction_db.csv materialized ($(stat -c%s "$WT/etk_reaction_db.csv") bytes)"
    fi
done
echo "done"
