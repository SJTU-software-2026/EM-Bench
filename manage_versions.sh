#!/usr/bin/env bash
# EM-Bench 版本管理入口：灵活添加/删除/恢复/查看 benchmark 版本。
#
# 用法:
#   bash manage_versions.sh                 # 无参数直接进入交互菜单（推荐）
#   bash manage_versions.sh menu            # 同上
#   bash manage_versions.sh list [--all]
#   bash manage_versions.sh add <commit> <id> <label> [add_version.sh 参数]
#   bash manage_versions.sh remove <id> [--reason 说明] [--purge] [--clean-worktree] [--clean-results] [--yes]
#   bash manage_versions.sh restore <id> [--new-id id] [--has-etk] [--reflect] [--label l] [--date d]
#   bash manage_versions.sh sync [--commits N]
#
# remove 默认是「软移除」：版本移入 versions.json 的 excluded_archived（保留 commit
# 记录与 reason），worktree 与 results/ 原样保留（只是不再参与后续评测；历史快照
# history/ 不受影响）。加 --purge 才从 JSON 彻底删除；加 --clean-worktree /
# --clean-results 才清理本地产物（删除结果数据需 --yes 或交互确认）。
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
CMD="${1:-menu}"
shift || true

case "$CMD" in
  menu)
    python3 "$ROOT/code/version_menu.py"
    ;;
  list)
    python3 "$ROOT/code/manage_versions.py" list --all "$@"
    ;;
  add)
    OUT=$(python3 "$ROOT/code/manage_versions.py" add "$@")
    rc=$?
    echo "$OUT"
    if [ $rc -ne 0 ]; then exit $rc; fi
    ID=$(echo "$OUT" | sed -n 's/^ADDED_ID //p')
    [ -z "$ID" ] && { echo "!! 无法解析新版本 id"; exit 1; }
    # 检出 worktree + 资产软链（幂等，已存在的版本跳过）
    echo "== 检出 worktree + 资产软链（幂等） =="
    bash "$ROOT/code/setup_versions.sh" || exit 1
    echo "== preflight（新版本） =="
    source "$ROOT/code/env.sh" 2>/dev/null || true
    ROOT="$(cd "$(dirname "$0")" && pwd)"   # env.sh（conda_setup）会覆盖 ROOT，恢复
    python3 "$ROOT/code/preflight.py" --versions "$ID" --out "$ROOT/results/preflight.json" || {
      echo "!! preflight 存在 fatal 问题（见 results/preflight.json）——先解决再跑基准。"
      exit 1
    }
    echo "完成。运行该版本基准: bash $ROOT/run_all.sh --versions $ID"
    ;;
  remove)
    python3 "$ROOT/code/manage_versions.py" remove "$@"
    rc=$?
    if [ $rc -eq 0 ]; then
      echo "提示: 若未加 --clean-worktree，versions/<id>/ 仍保留；"
      echo "      重新评测请用 bash run_all.sh --versions <版本列表>。"
    fi
    exit $rc
    ;;
  restore)
    OUT=$(python3 "$ROOT/code/manage_versions.py" restore "$@")
    rc=$?
    echo "$OUT"
    if [ $rc -ne 0 ]; then exit $rc; fi
    ID=$(echo "$OUT" | sed -n 's/^RESTORED_ID //p')
    [ -z "$ID" ] && { echo "!! 无法解析恢复后的版本 id"; exit 1; }
    # 恢复后的版本需要 worktree 与资产（幂等，已存在的版本跳过）
    echo "== 重建 worktree + 资产软链（幂等） =="
    bash "$ROOT/code/setup_versions.sh" || exit 1
    echo "== preflight（恢复版本） =="
    source "$ROOT/code/env.sh" 2>/dev/null || true
    ROOT="$(cd "$(dirname "$0")" && pwd)"   # env.sh（conda_setup）会覆盖 ROOT，恢复
    python3 "$ROOT/code/preflight.py" --versions "$ID" --out "$ROOT/results/preflight.json" || {
      echo "!! preflight 存在 fatal 问题（见 results/preflight.json）。"
      exit 1
    }
    echo "完成。运行: bash $ROOT/run_all.sh --versions $ID"
    ;;
  sync)
    python3 "$ROOT/code/manage_versions.py" sync "$@"
    exit $?
    ;;
  -h|--help|'')
    grep '^#   ' "$0" | sed 's/^#   //'
    ;;
  *)
    echo "未知子命令: $CMD"
    grep '^#   ' "$0" | sed 's/^#   //'
    exit 1
    ;;
esac
