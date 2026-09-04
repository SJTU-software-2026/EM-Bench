#!/bin/bash
# EM-Bench enzyme-tk 轨道（reaction_etk_ec）批量运行
# 用法: sbatch --export=ALL,BASE=<版本偏移> slurm/run_benchmark_etk.sbatch
# 每版本一个作业：--array=1-8%1（版本内串行）；BASE = 版本在 etk 列表中的序号*8，
# 即 v2=0, v3=8, v4=16, v5=24（v1_main 无 etk，被跳过）。
#SBATCH --job-name=embench_etk
#SBATCH --array=1-8%1
#SBATCH --partition=GuLab
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8

#SBATCH --time=03:00:00
#SBATCH --output=/data/gulab/igem2026/data10/EM-Bench/slurm_logs/etk_%A_%a.out
#SBATCH --error=/data/gulab/igem2026/data10/EM-Bench/slurm_logs/etk_%A_%a.err

set -u
BASE="${BASE:-0}"
source /data/gulab/igem2026/data10/EM-Bench/code/env.sh

cd /data/gulab/igem2026/data10/EM-Bench/code || exit 1
python run_task.py --array-idx "$((SLURM_ARRAY_TASK_ID + BASE))" --track etk --results ../results
exit $?
