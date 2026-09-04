#!/bin/bash
# EM-Bench 评估：在所有运行完成后生成指标、工具链报告与图
# 用法（在所有 run 任务结束后执行）:
#   sbatch slurm/run_evaluate.sbatch
# 或等待前置任务完成:
#   sbatch --dependency=afterok:<主流程jobid>,<etk jobid> slurm/run_evaluate.sbatch
#SBATCH --job-name=embench_eval
#SBATCH --partition=GuLab
#SBATCH --cpus-per-task=4
#SBATCH --time=01:00:00
#SBATCH --output=/data/gulab/igem2026/data10/EM-Bench/slurm_logs/evaluate_%j.out
#SBATCH --error=/data/gulab/igem2026/data10/EM-Bench/slurm_logs/evaluate_%j.err

set -u
source /data/gulab/igem2026/data10/EM-Bench/code/env.sh

cd /data/gulab/igem2026/data10/EM-Bench/code || exit 1
python evaluate.py --results ../results --out-dir ../results/report
exit $?
