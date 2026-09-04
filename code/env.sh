#!/usr/bin/env bash
# EM-Bench 环境初始化 — 内容取自 /data/gulab/igem2026/data10/prepare_env。
#
# 与 prepare_env 的唯一差异：
#   prepare_env 结尾的 `cd .../data11/igem_software/enzyme_update && python run.py`
#   是交互式启动；EM-Bench 将其替换为 benchmark 自身的运行入口
#   （code/run_task.py，经 slurm/*.sbatch 调用）。
#
# 使用方式：在 benchmark 运行 enzyme_update 之前 source 本文件，
# 例如 slurm 脚本中的 `source /data/gulab/igem2026/data10/EM-Bench/code/env.sh`。

# prepare_env 第 1 行：默认 GPU 0；若由 Slurm 分配 GPU 则保留 Slurm 的赋值
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
# prepare_env 第 2-3 行
export KMP_DUPLICATE_LIB_OK=TRUE
export MKL_THREADING_LAYER=GNU
# prepare_env 第 4 行
source /data/gulab/igem2026/data11/igem_software/envs/conda_setup_x86.sh
# prepare_env 第 5 行
conda activate /data/gulab/igem2026/data11/igem_software/envs/miniprot
# prepare_env 第 6 行
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"

# EM-Bench 附加：所需模型（ESM-C/prot_t5/molt5/rxnfp）均已预缓存到
# $HOME/.cache/huggingface 或随包分发。强制离线可避免 HF Hub 元数据校验
# 撞上外网抖动（实测在线校验会挂起 >120s，导致 ESM-C 加载失败）。
# EM-Bench 附加：节点本地 $HOME 的 HF 缓存互不可见（node9 实测为空），
# 将 HF 缓存重定向到共享目录（assets/hf_cache，ESM-C 600M 已入库），
# 否则非 node1 节点上 EnzymeCAGE 的 ESM-C 加载失败。
export HF_HOME=/data/gulab/igem2026/data10/EM-Bench/assets/hf_cache
export HF_HUB_CACHE=/data/gulab/igem2026/data10/EM-Bench/assets/hf_cache/hub
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
