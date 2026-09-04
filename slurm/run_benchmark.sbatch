#!/bin/bash
# EM-Bench 主流程（reaction_full）批量运行
# 用法: sbatch --export=ALL,BASE=<版本偏移> slurm/run_benchmark.sbatch
# 每版本一个作业：--array=1-8%1（%1 = 版本内严格串行，避免 enzyme_update 的
# 共享 state/session 文件在并发下互相覆盖）；BASE = (版本序号-1)*8，
# 即 v1=0, v2=8, v3=16, v4=24, v5=32。全局索引 = BASE + SLURM_ARRAY_TASK_ID。
#SBATCH --job-name=embench
#SBATCH --array=1-8%1
#SBATCH --partition=GuLab
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8

#SBATCH --time=03:00:00
#SBATCH --output=/data/gulab/igem2026/data10/EM-Bench/slurm_logs/run_%A_%a.out
#SBATCH --error=/data/gulab/igem2026/data10/EM-Bench/slurm_logs/run_%A_%a.err

set -u
BASE="${BASE:-0}"
# 使用 enzyme_update 前先运行 prepare_env 的内容（CUDA_VISIBLE_DEVICES 由 Slurm 分配，
# env.sh 仅在未设置时默认 0，与 prepare_env 一致）
source /data/gulab/igem2026/data10/EM-Bench/code/env.sh

cd /data/gulab/igem2026/data10/EM-Bench/code || exit 1
python run_task.py --array-idx "$((SLURM_ARRAY_TASK_ID + BASE))" --track primary --results ../results
exit $?
