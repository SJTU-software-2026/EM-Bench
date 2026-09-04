#!/usr/bin/env python3
"""run_all.sh 辅助：输出各轨道的 (版本 id, BASE 偏移, 数组大小) 清单。

与 run_task.py 的 combos 顺序保持一致：
  primary: 全部版本 × 任务（BASE = 版本序号*任务数）
  etk:     has_etk 版本 × 任务
用法:  python3 _bases.py [--track primary|etk] [--versions v1_main,v2_etk]
输出:  每行 "版本id BASE 数组大小"
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    args = sys.argv[1:]
    track = "primary"
    wanted = ""
    i = 0
    while i < len(args):
        if args[i] == "--track":
            track, i = args[i + 1], i + 2
        elif args[i] == "--versions":
            wanted, i = args[i + 1], i + 2
        else:
            i += 1

    versions = json.load(open(ROOT / "code" / "versions.json"))["versions"]
    if wanted:
        ids = {v.strip() for v in wanted.split(",") if v.strip()}
        versions = [v for v in versions if v["id"] in ids]
    n = len(json.load(open(ROOT / "tasks" / "tasks.json"))["tasks"])

    if track == "etk":
        versions = [v for v in versions if v.get("has_etk")]
    for i, v in enumerate(versions):
        print(v["id"], i * n, n * len(versions))


if __name__ == "__main__":
    main()
