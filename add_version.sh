#!/usr/bin/env bash
# 兼容壳：add_version.sh 的功能已并入 manage_versions.sh add（校验 commit →
# 追加 versions.json → worktree + 资产 → preflight）。请改用:
#   bash manage_versions.sh add <commit> <id> <label> [--ref ref] [--date date] [--features 说明] [--no-etk] [--reflect]
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec bash "$ROOT/manage_versions.sh" add "$@"
