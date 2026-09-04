# EM-Bench：enzyme_update 多版本酶挖掘 Benchmark

参照 **EC-Bench**（统一平台、标准化数据与任务、性能/资源/一致性三维指标、模型可插拔）
的架构与原则，并结合 `deep-research-report.md` 的 prospective top-k 酶挖掘评价框架，
对 **enzyme_update（Owl-Eyezyme / MiniProt Virtual Lab）的不同历史版本** 在 **8 个
文献酶挖掘任务** 上进行标准化评测。每个任务记录版本实际执行的**具体工具链调用**，
并用检索指标（Hit@k / Precision@k / EF@k / nDCG@k）、资源指标与版本间一致性给出评价。

## 目录结构

```text
EM-Bench/
├── tasks/
│   ├── tasks.json          # 8 个标准化任务：反应 SMILES、文献条件、锚点酶、难度
│   └── anchors.json        # 锚点酶 UniProt 元数据（fetch_anchors.py 生成）
├── code/
│   ├── env.sh              # prepare_env 内容（使用 enzyme_update 前 source）
│   ├── versions.json       # 被评测版本（含 search_space 元数据）+ 排除的归档分支
│   ├── fetch_anchors.py    # 构建：RDKit 校验 SMILES + 抓取锚点元数据
│   ├── setup_versions.sh   # 构建：git worktree 检出各版本 + .env + 大文件软链
│   ├── link_assets.py      # setup 辅助：未入库权重/模型软链
│   ├── run_task.py         # 运行器：单版本×单任务，记录工具链/时间/资源
│   └── evaluate.py         # 评估器：指标、工具链报告、一致性、图、总报告
├── slurm/
│   ├── run_benchmark.sbatch       # 主流程数组任务（版本数×8，reaction_full, GPU）
│   ├── run_benchmark_etk.sbatch   # etk 轨道数组任务（有 etk 的版本×8，reaction_etk_ec）
│   └── run_evaluate.sbatch        # 评估（可加 --dependency=afterok）
├── versions/               # 各版本的 git worktree（setup_versions.sh 生成）
└── results/                # 运行结果 + report/（指标、图、报告）
```

## 被评测版本（enzyme_update）

| 版本 | 提交 | 日期 | 新增能力 | 搜索空间（search_space） |
|---|---|---|---|---|
| `v1_main` | 6399e1e | 2026-06-17 | 基线：CLAIRE+CAGE 固定流程 + enzyme_evaluation | swissprot_only |
| `v3_more` | d819414 | 2026-07-29 | + 文献/EPMSSA/DBM/酶改造流程 | swissprot_only |
| `v5_evolve` | 6a6a6eb | 2026-08-22 | + 定向进化（ProteusAI MCMC + CataPro） | swissprot_only |
| `correct_cage` | 092eb55 | 2026-09-03 | + goal 文本驱动检索链（关键词 + EC 过滤 + 自适应搜索） | swissprot_trembl_fallback |

归档分支（claire_cage、enzyme-evaluation、demand-vector-eval、paper-rag、config_fix）
因早于统一 pipelines.yaml/CLI 或已被并入上述版本而排除，理由见 `code/versions.json`。

## 任务（源自 deep-research-report 的 8 个核心 benchmark）

| 任务 | 反应 | 锚点酶（grade） | 难度 | 检测 |
|---|---|---|---|---|
| t1_abts | ABTS → ABTS•⁺ | P07788 CotA(2), D0VWU3 Trametes laccase(2) | low | A420 |
| t2_trp_indole | L-Trp → indole | P0A853 TnaA/EcoTIL(2) | low | HPLC-UV |
| t3_co2_hco3 | CO₂ → HCO₃⁻ | P00918 hCA2(2), P00921 bCA2(2) | low | pH/stopped-flow |
| t4_vg_vanillin | 4-VG → vanillin | G2QIL8(2)；CSO2/SsCSO 不在 UniProtKB | medium | A480+LC-MS |
| t5_pet_hydrolysis | PET 二聚体水解 | A0A0K8P6T7 IsPETase(2), G9BY57 LCC(2)；KbPETase 不在 UniProtKB | high | UPLC |
| t6_asp_chloro | L-Asp → (3S)-Cl-Asp | A0AAC9SM19 AspX(3, TrEMBL) | high | LC-MS |
| t7_biotin_chloro | biotin → (2R)-chlorobiotin | A8LT50 BtnX(3) | high | LC-MS/QTOF |
| t8_dehp_hydrolysis | DEHP → 单酯 | Q7SIG1(3, TrEMBL) | medium | LC-MS |

- grade：2 = 论文 reference 酶；3 = 论文挖到的 discovery 酶（AspX/BtnX/Q7SIG1）。
- **锚点可达性（按版本搜索空间动态计算）**：旧版本候选搜索用
  `uniprot_search(reviewed_only=True)`（仅 Swiss-Prot，search_space=
  `swissprot_only`）；correct_cage（092eb55）的检索链为 Swiss-Prot 优先、
  空结果自动回退 TrEMBL（`_uniprot_search_adaptive`，search_space=
  `swissprot_trembl_fallback`）。因此 TrEMBL 锚点（A0AAC9SM19/A8LT50/Q7SIG1）
  对旧版本不可达、对 correct_cage 按能力计入 reachable 变体——是否真的触发
  回退以各运行的 `trEMBL_anchor_in_pool` 字段实证。评测同时报告
  `reachable`（该版本搜索空间可达锚点）与 `all`（全部锚点，跨版本统一口径）
  两种变体；无湿实验活性时以 all 变体为准。

## 评价指标

**检索（performance，EC-Bench 维度一；公式见 deep-research-report）**

| 指标 | 说明 |
|---|---|
| Hit@k (k=1,3,5,10) | top-k 是否命中任一锚点（grade≥2） |
| Precision@k | top-k 中锚点占比 |
| MRR / BestRank | 首个锚点的倒数排名 / 最佳排名 |
| nDCG@10 | 等级 0/2/3 的折损增益（奖励把 grade-3 锚点排在前列） |
| PoolRecall | 锚点是否进入候选池（`uniprot_search` 阶段） |
| EF@k | 富集因子：Precision@k ÷ 同池随机基线（Monte Carlo 2000 次抽取，seed=42） |
| Spearman ρ | CAGE 预测分与锚点等级的相关性（仅主流程；干实验代理） |

**资源（EC-Bench 维度二）**：单任务 wall time、进程树峰值 RSS、GPU 峰值显存、输出体积。

**一致性（EC-Bench 维度三）**：版本间 top-10 候选集合的成对 Jaccard 与 agreement rate；
另有各版本 `config/pipelines.yaml` 的能力矩阵。

**工具链记录（本 benchmark 核心交付物）**：每次运行按步骤记录
`step(工具, 状态, 耗时)` 序列、EC 查询线索、候选池大小与 top-10（锚点高亮），
汇总于 `results/report/toolchain.md`，逐步骤时间线见图 `fig2`。

**综合 100 分（仅 dashboard）**：Hit@10×30 + nDCG@10×20 + BestGrade@10×20 +
EF@10×15 + Precision@10×10 + ρ×5；正式比较一律用原始指标。

## 使用流程

```bash
# 0) 构建：RDKit 校验任务 SMILES + 抓取锚点元数据（需网络，miniprot 环境）
source code/env.sh                     # prepare_env 内容
python code/fetch_anchors.py

# 1) 准备各版本环境（git worktree + .env + 权重软链）
bash code/setup_versions.sh

# 2) 提交运行（大程序用 sbatch）
sbatch slurm/run_benchmark.sbatch          # 主流程数组任务（版本数 × 8）
sbatch slurm/run_benchmark_etk.sbatch      # etk 轨道数组任务（有 etk 的版本 × 8）

# 3) 全部完成后评估（也可用 --dependency=afterok:<jobid> 自动衔接）
sbatch slurm/run_evaluate.sbatch

# 4) 查看结果
#    results/report/report.md、toolchain.md、metrics.csv、figures/*.png
```

## 一键全自动流程（推荐，用于版本更新后的复评）

`run_all.sh` 把上述 2)-4) 步编排为一条命令：**preflight 自检+自动修复 → sbatch
提交（每版本×轨道一个数组，版本内 %1 串行）→ 轮询等待 → cage 特征审计 → 评估
→ 快照到 history/ → 与上次运行自动对比生成进步报告**。

```bash
# 全量复评（全部版本、primary + etk 两轨道）
bash run_all.sh

# 只评新版本 / 指定版本
bash run_all.sh --versions v6_future
bash run_all.sh --versions v1_main,v6_future --tracks primary

# 先看将执行的命令（不实际提交）
bash run_all.sh --dry-run

# 环境问题修复后只重跑失败/缺失任务（成功任务秒跳，不再重算）
bash run_all.sh --retry-failed
```

### 更新 enzyme_update 后追加新版本

GitHub 有新提交**不会自动进入 benchmark**（版本钉死在 `code/versions.json` 的
commit hash 上，且 `add` 只认本地 clone 中已存在的 commit）。两步流程：

```bash
# 1) 同步远端（fetch origin --prune + 展示新提交；远端无变化时明示）
bash manage_versions.sh sync

# 2) 把新提交纳入 benchmark（校验 commit 存在于本地仓库 → versions.json →
#    worktree + 资产软链 → preflight 自检）
bash manage_versions.sh add <commit> <id> <label> [--ref main] [--features "新能力说明"] [--reflect]

bash run_all.sh --versions <id>      # 跑新版本基准
```

交互菜单 [6] 可一步完成 sync，并在有更新时直接引导添加版本。（`add_version.sh`
保留为一行兼容壳，转发到 `manage_versions.sh add`。）

## LLM 撰写评价解读（可选）

脚本只自动生成**数据报告**（report.md / toolchain.md / metrics.csv）；历史解读
docs/evaluation_notes.md 是手工维护的。可选地让大语言模型基于运行数据撰写解读：

```bash
# 1) 配置凭据（与 enzyme_update 的 .env 同款用法；.env 已被 .gitignore 忽略，绝不入库）
cp .env.example .env              # 填入真实 key；脚本运行时自动加载（已存在环境变量优先），无需 source
#   可写 EMBENCH_LLM_BASE_URL / EMBENCH_LLM_API_KEY / EMBENCH_LLM_MODEL（EM-Bench 专属），
#   或直接粘贴 enzyme_update .env 的 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
#   ——两种写法等价，两项目共用同一 API。端点（Anthropic 原生 / OpenAI 兼容自动识别；
#   node1 实测可达：api.anthropic.com、api.deepseek.com、api.moonshot.cn、
#   dashscope.aliyuncs.com；api.openai.com 被墙）；key 日志中自动掩码，不写入任何文件。

# 1') 不建 .env 时，脚本按下列优先级解析（任一来源即可，key 永远只在本机）：
#       EMBENCH_LLM_* 环境变量（显式覆盖）
#       → enzyme_update 仓库 config/settings.yaml(llm:) → .env(DEEPSEEK_*)
#       → DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL 环境变量
#       → 提供商预设（默认 deepseek）
#     仓库路径取 versions.json 的 repo 字段（可用 --enzyme-repo 覆盖）；
#     只提取 provider/api_key/model/base_url 四个字段，绝不打印明文、不落盘。

# 2) 生成（读 results/ → 压缩为数据摘要 → 调 LLM → 写 evaluation_notes_llm.md）
python3 code/write_notes_llm.py

# 不调 API 的用法：
python3 code/write_notes_llm.py --dry-run        # 只生成 prompt（llm_prompt.md），人工审查
python3 code/write_notes_llm.py --dump-digest    # 只导出摘要（llm_digest.md），可交给任何 LLM/人工
# 无任何凭据时脚本只生成摘要后优雅退出（exit 0），benchmark 核心流程不受影响。
```

要点：
- 摘要含 benchmark_version、版本集/search_space、逐任务×版本指标、候选通道分布
  （ec/keyword/text/rhea）、trEMBL_anchor_in_pool、step_failures——与 evaluation_notes.md
  的证据口径一致；提示词硬性要求「数字只能来自摘要、禁止编造、归因引用证据字段」。
- 输出写 **evaluation_notes_llm.md**，绝不覆盖手工维护的 evaluation_notes.md；
  生成文件头记录模型/端点/数据快照/提示模板版本，正文标注「请人工核对后采信」。
- `run_all.sh` 在快照前自动执行该步骤（只要本仓库 .env、EMBENCH_LLM_* / DEEPSEEK_*
  环境变量或 enzyme_update 的 settings.yaml/.env 任一存在；失败不阻塞）。生成的
  llm_digest.md / evaluation_notes_llm.md 随快照一起归档。

## 版本管理（灵活添加/删除）

统一入口 `manage_versions.sh`（底层为 `code/manage_versions.py`）。**无参数直接进入
终端交互菜单**（零依赖，SSH 友好）：查看 / 添加 / 删除 / 恢复版本、一键跑基准，
全程问答式操作并自动完成 worktree 检出与 preflight 自检：

```bash
bash manage_versions.sh          # 交互菜单（推荐）
```

菜单项：[1] 查看版本（含结果统计与归档）· [2] 添加版本（自动展示仓库最近提交，
可输编号）· [3] 删除版本（软移除/彻底删除 + 可选清理，均有确认）· [4] 恢复归档
版本 · [5] 一键运行基准（run_all.sh）· [6] 同步 GitHub（fetch origin --prune，
展示新提交并可立即添加版本）· [7] 最近提交（全部引用，含提交时间）·
[8] 全部版本（分支/标签 tip 一览，标注 [已评测]/[已归档]）。

也可用子命令方式（脚本化调用）：

```bash
# 查看所有版本：worktree / 结果统计 / 能力标记
bash manage_versions.sh list

# 添加版本（完整环境准备：JSON + worktree + 资产 + preflight）
bash manage_versions.sh add <commit> <id> <label> [--ref main] [--no-etk] [--reflect]

# 软移除（默认）：版本移入 versions.json 的 excluded_archived（保留 commit 记录
# 与 reason），worktree 与 results/ 原样保留，只是退出后续评测；
# history/ 历史快照不受影响
bash manage_versions.sh remove v3_more --reason "实验性流程，暂时移出"

# 彻底删除版本条目（+ 可选清理本地产物）
bash manage_versions.sh remove v3_more --purge
bash manage_versions.sh remove v3_more --purge --clean-worktree --clean-results --yes

# 从归档恢复（自动重建 worktree + preflight 自检）
bash manage_versions.sh restore v3_more --has-etk

# 同步 GitHub（fetch origin --prune，汇总新提交；不修改版本配置）
bash manage_versions.sh sync
```

要点：
- **删除/添加版本后数组 BASE 会自动重算**（`_bases.py` 按 versions.json 动态生成），
  无需手改任何 sbatch 配置；`results/<版本id>/` 按版本名存放，与顺序无关。
- `--clean-results` 删除结果数据前会列出将删内容并要求确认（非交互下需 `--yes`）。
- `--clean-worktree` 使用 `git worktree remove --force`（版本 worktree 可能因
  preflight 修复过符号链接而脏）。
- 版本集变化会被 progress_report.py 识别为「新增/移除版本」，出现在进步报告中。

### preflight 自检（把首轮排障经验固化为自动检查）

`code/preflight.py` 在每次运行前自动检查并（默认）修复：
1. **符号链接陷阱**（v1 全败根因）：EnzymeCAGE 管线 6 文件的符号链接会因
   `Path(__file__).resolve()` 与 CPython 主脚本 `sys.path[0]` realpath 穿透到
   原始安装（其 rxnmapper 权重/dataset 为悬空链接）→ 自动替换为真实文件；
2. rxnmapper 权重链接存活、`enzymecage.dataset` 可导入（enzymecage 解释器轻量探测）；
3. CAGE checkpoint / p2rank / HF 共享缓存（ESM-C 离线）/ .env 存在；
4. enzymecage python、miniprot 环境、JAVA（p2rank 依赖）。

### 进步观察报告

每次 `run_all.sh` 结束把完整报告快照存入 `history/run_<时间戳>/`（含
`run_meta.json`：版本集指纹、commit 映射、Slurm jobids、成功/失败统计），并自动
对比最近两次快照生成 `history/progress_<旧>_vs_<新>.md`：版本集变化、任务级
Hit@10/nDCG/PoolRecall 的 ↑↓ 对比与汇总。指定历史对比：

```bash
python code/progress_report.py --history history --old run_20260829_120000 --new run_20260829_180000
```

> 每次使用 enzyme_update 前必须先 source `code/env.sh`（即 prepare_env 内容：
> CUDA_VISIBLE_DEVICES/KMP/MKL 环境变量 + conda_setup_x86.sh + miniprot 环境 +
> LD_LIBRARY_PATH）。与 prepare_env 的差异仅是结尾的交互式 `python run.py`
> 被替换为 benchmark 入口（见 env.sh 注释）。Slurm 下 GPU 由 `--gres=gpu:1`
> 分配，env.sh 只在未设置时默认 0。

## 资产部署与数据来源（provenance）

大文件（权重/模型/反应库）不入 git；`setup_versions.sh` 对每个版本 worktree 按
**部署副本 → 主仓库 → assets shim** 三遍执行 `link_assets.py`，逐文件软链未入库
资产（git 跟踪的 vendored 代码保持检出版本）：

| 资产 | 来源 | 备注 |
|---|---|---|
| Pfam-A.hmm、p2rank_2.5.1+Java、CAGE `epoch_19.pth`、rxnmapper bins | 部署副本 `data11/igem_software/enzyme_update` | 软链 |
| ESM-C 600M | HuggingFace 本地缓存 | CAGE 运行时加载 |
| CLAIRE 数据 `dev/data/` | Zenodo **14635841** `data.zip`（921,446,213 B） | 并行下载曾被 CDN 缓存副本污染；最终以单连接整段重下 + `unzip -t` 62/62 无错校验 |
| CLAIRE 模型 3 个 `.pth` | github.com/zishuozeng/CLAIRE `dev/results/model/` | `layer5_node1280_ec{1,2}_triplet2000_final.pth` 与 ec123 |
| `etk_reaction_db.csv` | git-lfs 指针（134 B）→ 直接拷贝 data11 仓库 `.git/lfs/objects/d6/c9a8/…` 物化（129,899,301 B） | 本机无 git-lfs 二进制，`setup_versions.sh` 末尾做对象级物化 |

## 运行协议（防止作弊与偏差）

1. **同一查询、同一参数**：所有版本用相同的 `-r '<goal 描述> <SMILES>' --phase all
   --pipeline reaction_full --max-candidates 20 --no-cache`（etk 轨道为
   reaction_etk_ec），不针对版本调整参数。goal 描述为**底物级目标文本**
   （tasks.json 的 goal_text 字段），供新版检索链提取底物关键词（ABTS/PET 等）；
   旧版本只从中提取 SMILES，文本部分不影响其候选检索（结果与历史协议可比）。
2. **缓存关闭**（--no-cache）：每次运行真实执行工具，保证工具链记录完整。
3. **失败即记录**：步骤失败/超时/无会话都写入 result.json 的 failure_type，
   失败任务按 0 命中计入汇总（不静默丢弃）。
4. **锚点盲测**：任务只给反应 SMILES + 底物级目标描述（goal_text）；锚点仅在
   评估阶段注入（本仓库为本地数据，无联网检索泄漏问题——文献名、命中酶、
   EC 号与 accession 均不写入查询；goal_text 只含底物/反应描述）。
   evaluate.py 内置盲测 lint（EC 编号 / UniProt accession / 锚点酶名黑名单），
   结果写入 metrics.csv 的 goal_lint 列并在报告 §1 告警。
5. **版本间可比性**：同一任务同一查询；候选搜索空间按版本记录
   （versions.json search_space：swissprot_only / swissprot_trembl_fallback），
   锚点可达性按该元数据动态计算，all-anchor 变体为跨版本统一口径；
   版本新增的流程（reflect/etk/检索链重构）是其评测内容的一部分。
6. **协议版本记录**：tasks.json benchmark_version + protocol_changelog 记录
   调用协议变更（1.0 仅 SMILES → 1.1 goal 描述+SMILES）；每次运行快照的
   run_meta.json 记录所用协议版本，progress_report.py 在两侧协议不同时
   显式警告（指标差异可能来自协议变更而非版本进步）。

## 已知局限

- 干实验 benchmark：无湿实验活性数据，等级为锚点 grade 代理；报告中的
  BRA（best relative activity）以 BestGrade@10 替代。
- swissprot_only 版本的候选空间使 t6/t7/t8 的 discovery（TrEMBL）锚点不可达；
  correct_cage 的 TrEMBL 回退是否真的触发以 trEMBL_anchor_in_pool 实证为准（如实报告）。
- t1（ABTS 自由基）与 t5（聚合物）的 SMILES 为形式化/二聚体替代，已在任务
  元数据中注明；t4/t5 部分文献锚点不在 UniProtKB。
- 时间/资源为单次观测；多重复与统计检验需扩展（见 deep-research-report §统计设计）。
- **etk_clean 步骤在所有版本一致失败**：`.env` 的 `CLEAN_DIR` 为占位符
  `/path/to/CLEAN`，且本机无 `clean` conda 环境与 CLEAN 权重。属部署缺口而非
  版本回归（对所有版本影响相同，reaction_etk_ec 其余步骤正常，pipeline_success
  仍为 True）；CLEAN 的 EC 交叉验证功能在 etk 轨道中视为未启用。
- v4_reflect 的 report/JSON 输出路径存在数据目录重复拼接的外观 bug
  （`data/gulab/…/data`），不影响会话落盘与评测。


## 引用

- EC-Bench: Davoudi et al., *Bioinformatics Advances*, doi:10.1093/bioadv/vbag004 (2026)
- 任务来源文献见 `tasks/tasks.json` 各任务的 source 字段与 `deep-research-report.md`
