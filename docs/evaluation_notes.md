# EM-Bench 评价解读（手工追加章节）

对 report.md 中指标的含义、归因与结论的补充解读。手工维护，不会被 evaluate.py 覆盖。

## 1. 核心结果一览

**1.1 协议全量复评（run_20260903_184403，56/56 运行成功）**

| 结论 | 证据 |
|---|---|
| 旧版本（v1_main / v3_more / v5_evolve）主流程仍全 0 —— 与 1.0 协议结果逐项一致（协议兼容性成立） | progress 报告 0.000 (=)，8 任务 × 3 版本 |
| **correct_cage（092eb55）在 t1（ABTS 氧化）与 t5（PET 水解）上 Hit@10=1.0、PoolRecall=1.0**（1.0 协议下全 0） | metrics.csv correct_cage 行：t1 best_rank=6（P07788），t5 best_rank=3（G9BY57） |
| correct_cage 综合分 **11.0** vs 旧版本 2.5；版本间 top-10 Jaccard 从 1.0 → **0.530**（新检索链带来真实版本差异，benchmark 区分度恢复） | composite.csv / consistency.csv |
| etk 轨道 3 版本一致：仅 t5 命中（与 1.0 协议相同） | metrics.csv etk 行 |
| t6 的 discovery 锚点 A0AAC9SM19（AspX）已被 UniProtKB **删除**（DELETED，"Not part of a reference proteome"）——任何版本、任何搜索模式都无法捞到；t6 discovery 评分在补到后继 accession 前不可解 | UniProt REST API / anchors.json entryType=Inactive |

<details>
<summary>1.0 协议时代的结果（2026-08-29 快照，已被 1.1 复评取代）</summary>

| 结论 | 证据 |
|---|---|
| 所有版本在 8 任务 × 2 轨道全部**运行成功**（72/72） | report.md 各表 成功/失败 = 8/0 |
| 主流程（reaction_full）在 8 任务上 **Hit@10 = 0.0，PoolRecall = 0.0**（全部版本） | metrics.csv pool_recall_reachable 全 0 |
| etk 轨道（reaction_etk_ec）在 t5（PET 水解）上命中 PETase（A0A0K8P6T7），Hit@10=0.125，版本间一致 | metrics.csv etk 行 hit10_all=1.0（t5），pool_recall_all=0.5 |
| 版本间 top-10 候选集完全一致（Jaccard=1.0） | consistency.csv / report.md §5 |

</details>

## 2. 为什么主流程全 0：候选池瓶颈（已核实到代码级证据）

> **本节基于 1.0 协议（`-r` 仅传 SMILES）的 2026-08-29 快照。** 1.1 协议
> （`-r '<goal 描述> <SMILES>'`）复评后的归因更新见 §6；1.0 协议下
> "0 命中根因在检索设计而非 benchmark" 的结论只对旧版本成立——
> 对 092eb55 这类以 goal 文本为驱动的检索链，1.0 协议漏传了其核心输入，
> 属协议缺陷（详见 §6 问题 1）。

主流程挖掘链 = CLAIRE 预测 EC → UniProt 按 EC 检索 20 个候选（reviewed_only=True）→ CAGE 几何+序列+反应指纹打分排序 → top-10。**0 命中发生在检索阶段而非排序阶段**。以 t1（ABTS 氧化，锚点漆酶 CotA P07788 / LAC1 D0VWU3，真实 EC 1.10.3.2）为例的证据链：

1. **CLAIRE 的预测就不对**：v5 的 evidence_memory.json 记录 `predicted_ecs = [1.5.99, 1.7.2, 1.7.1, 1.13.12, 1.1.5]`——没有一个接近真实的 1.10.3.2。
2. **查询前被截断到一级类目**：CLAIRE 工具把查询提示压缩为 `ec_hints = ["1.1"]`（executor.py 用 `hierarchy["uniprot_query_hints"]`），随后 `_search_uniprot_by_ecs` 以 `query="ec:1.1"` + `reviewed_only=True` + `limit≤20` 检索 UniProt（src/orchestration/executor.py:243-286）——把 CLAIRE 本来就不准的预测再截成一级类目，误差被放大。
3. **候选池因此全是 EC 1.1 家族**：返回的 20 个 Swiss-Prot 蛋白（P56937 3-oxoacyl-ACP reductase、P16152 carbonyl reductase 等）全部属于 1.1.x.x 脱氢/还原酶，与多铜氧化酶（1.10.3.2）风马牛不相及。
4. **CAGE 在池内正常排序**：特征完整（ESM-C 20/20 npz + GVP）、ranked.csv 有真实分数分布（pred 1e-5→1e-9 递减）——它在给定候选池上忠实完成了排序职责，但池里没有锚点可排。

**结论：这是原 enzyme_update 项目主流程的真实检索设计问题，不是 benchmark 流程错误**——benchmark 忠实执行了标准调用（`--phase all --pipeline reaction_full --max-candidates 20`），工具链全部真实运行并产出完整产物；0 命中的根因在「CLAIRE EC 预测不准 → 查询提示截断为一级 EC → reviewed_only 限制 → 20 条上限」这一检索链上。反证：etk 轨道（按 RDKit 反应指纹相似度检索，不依赖 EC 类目）在 t5 捞到锚点，说明换检索策略即可让锚点进池。

对 enzyme_update 项目的改进提示（供版本进步观察）：(a) 用 CLAIRE 的全级 EC 预测直接查询（不截断为一级）；(b) 提高 --max-candidates 或对多 EC 分别检索后合并；(c) 任务可选 reviewed_only=False。这些点的任何变化都会直接反映在 PoolRecall/Hit@k 上，正是本 benchmark 设计的观察维度。

## 3. etk 轨道的命中

- t5（PET 水解）etk 候选仅 1-2 个（BRENDA/EnzymeMap 按 RDKit 反应指纹相似度检索），其中 A0A0K8P6T7（IsPETase 变体，TrEMBL）被捞到 → 4 个版本一致命中（Hit@10=1.0 in all-anchor 口径）。
- etk 检索按**反应指纹相似度**而非 EC 类目，对 EC 错位类任务（如 PET 水解，锚点 EC 归属不清）更稳健——这解释了 etk > full 的差异。
- 注意 etk 轨道 `etk_clean` 步骤全部 failed（0.2-0.4s）但被 run.py 容忍（pipeline_success=True）：clean 失败未影响候选保留，属"静默降级"，已在工具链表中如实标注。
- 其余 7 任务 etk 候选池 1-5 个且无锚点——BRENDA/EnzymeMap 对非工业主流反应的覆盖有限。

## 4. 版本间"零差异"的解读

5 个版本主流程 top-10 完全一致（Jaccard=1.0）、指标全同。原因：
1. 各版本的 candidates（uniprot_search）与 cage（EnzymeCAGE 同源代码、同 checkpoint）完全相同；版本差异在流程编排（v3 加文献挖掘、v4 加 reflect、v5 加定向进化），这些能力在**固定 8 任务 + max-candidates 20 + 无湿实验**的设定下没有改变 top-20 候选与 CAGE 排序。
2. reflect（v4/v5）只给 EC 线索（如 t1 的 1.1），不改变候选池；定向进化（v5）在无湿实验活性反馈下不触发迭代收益。
3. 因此本基准对**版本间差异的区分度有限**；其真正价值在于：(a) 量化主流程候选池瓶颈；(b) 证明 etk 轨道在 EC 错位任务上的互补价值；(c) 建立可复用的工具链/资源/一致性基线，供后续"扩候选池、加湿实验 BRA"的迭代评测对照。

**CAGE 源码的版本间逐文件对比（worktree 实测，2026-09-03）**——佐证"零差异"不是采样噪声而是代码事实：

| 版本对 | EnzymeCAGE 树差异（.py） | 说明 |
|---|---|---|
| correct vs v5_evolve | **0 个文件** | 完全一致；correct（a5814d1, 07-29, test_merge bugfix）早于 v5（08-22），其 CAGE 改动（CUDA 回退等）在 v5 中同样存在 |
| correct vs v1_main | 仅 feature/main.py | correct 反而**删除**了 v1 的严格检查（ESM-C 0 特征即 RuntimeError）→ 健壮性放松而非功能改进 |
| v3_more vs 其余 | atom3d.py/schnet.py/utils.py | v3 缺 CUDA 回退与 SMILES 解析校验（分支分叉点），是唯一 outlier |

benchmark 行为证据（t1_abts ranked.csv）：4 版本 top-10 完全一致；correct 分数与 v1_main **bit 级一致**；v3/v5 仅有浮点抖动（v5 在 rank 11/12 平分区交换一对候选，top-10 之外）。**结论：correct 的 CAGE 无功能改进；即便有，主流程 0 命中根因在检索链（PoolRecall=0），CAGE 改进也不会转化为指标变化——观察版本进步应盯 PoolRecall。**

**建议的后续基准增强**（不改变本次结论）：`--max-candidates` 提到 100+ 看 PoolRecall 是否抬升；对 v4/v5 单独评估 reflect/evolve 对排序的增益；对 t5 类任务将 etk 与 cage 融合评估；记录节点名；重复运行取均值。

## 5. 资源画像要点

- cage（EnzymeCAGE 特征+推理）是资源大头：GPU 峰值 6.8-9.9 GB（ESM-C 600M + GVP + EnzymeCAGE 推理），RSS 峰值 4.5-9.0 GB。
- node1 CPU 慢路径下 wall 膨胀 6-7 倍（t2: 996-1016s vs GPU 89-168s），但成功——GPU 是效率必需品而非正确性必需品（v1 修复后）。
- v4/v5 的 reflect 步骤额外 +40-60s，但 top-10 无变化——reflect 的成本收益在本任务集上为负。

## 6. 协议审查（2026-09-03，全量复评前系统检查）

应"再检查 benchmark 协议"的要求，对运行协议做了系统审查，发现并修复 4 个问题：

**问题 1（严重，已修复）：1.0 协议漏传目标文本，新检索链被"饿死"。**
enzyme_update 交互式真实用法是「目标描述 + 反应 SMILES」。092eb55（correct_cage）
的新检索链以 goal 文本为驱动（uniprot_query.py：EC 是过滤器而非完整查询，
关键词来自 goal；goal_function_ecs 如 "laccase/ABTS→1.10.3"；关键词旁路检索）。
1.0 协议只传 `-r SMILES`，`goal = args.reaction` 拿到的只有 SMILES —— 旧版本
只提取 SMILES 不受影响，但新版本的核心检索输入缺失。1.0 时代
"0 命中根因在 enzyme_update 检索设计、benchmark 忠实执行标准调用"的结论
**只对旧版本成立**——这是协议设计疏漏，已公开承认。
修复：1.1 协议改为 `-r '<goal 底物级描述> <SMILES>'`（tasks.json goal_text，
只含底物/反应描述，盲测安全），旧版本从中只提取 SMILES，与 1.0 历史可比。
早期证据（correct_cage/t1_abts）：候选池从 20 → 29，P07788 与 D0VWU3
**双双进池**（1.0 时 PoolRecall=0），P07788 排第 6（Hit@10=1）。

**问题 2（严重，已修复）：锚点可达性是静态全局口径，对 fallback 版本错误。**
anchors.json 的 reachable 按「Swiss-Prot=可达」静态计算；但 092eb55 的
`_uniprot_search_adaptive`（executor.py:44-66）在 Swiss-Prot 空结果时自动回退
TrEMBL——TrEMBL 锚点（A0AAC9SM19 AspX / A8LT50 BtnX / Q7SIG1）对该版本
可能可达，旧口径会把它排除在 reachable 变体之外并把命中误标为 all 变体。
修复：versions.json 新增 search_space 元数据（swissprot_only /
swissprot_trembl_fallback，代码实证），evaluate.py 按版本×运行计算可达集合，
并新增 trEMBL_anchor_in_pool 字段记录 TrEMBL 锚点实际进池的实证；
all-anchor 变体保持跨版本统一口径。

**问题 3（中，已修复）：协议版本未记录，历史对比可能把协议变更误读为版本进步。**
修复：tasks.json benchmark_version 1.0→1.1 + protocol_changelog；
progress_report.py 在两次快照协议版本不同时给出显式警告。

**问题 4（低，已修复）：goal_text 无泄漏校验。**
修复：evaluate.py 对 tasks.json 的 goal_text 做盲测 lint（EC 编号 /
UniProt accession / 锚点酶名黑名单），结果写入 metrics.csv goal_lint 列
与报告 §1；本轮全量复评后将在 run_task.py 提交侧加同款告警，使泄漏在
提交队列前被拦截（避免浪费集群时间）。

其余核查结论：--no-cache 在 092eb55 仍被尊重（run.py:668 `use_cache = not
args.no_cache`）；同一查询同一参数、失败即记录、盲测锚点注入等既有条款
继续成立。

### 6.1 复评结果（run_20260903_184403，1.1 协议）

**命中通道实证（候选来源字段，session.json candidate_source）**——1.1 协议
把 goal 文本送进检索链后，correct_cage 的命中全部来自 1.0 协议下根本不存在的通道：

| 任务 | 锚点 | 来源通道 | 排名 |
|---|---|---|---|
| t1_abts | P07788（CotA 漆酶） | **keyword**（关键词旁路检索） | 6 |
| t1_abts | D0VWU3（Trametes 漆酶） | 进池未入排（CAGE 只排 9/29） | — |
| t5_pet | G9BY57（LCC 角质酶） | **keyword** | 3 |
| t5_pet | A0A0K8P6T7（IsPETase） | ec | 进池未入 top |
| t5_pet | A0A0K8P8E7（reviewed IsPETase 亲本，非锚点） | **text** | **1** |

候选来源分布也从"全部 ec"变为混合（t5: 5 keyword + 5 ec + 2 text；
t1: 6 keyword + 6 ec），证明 092eb55 的检索链确实是 goal 文本驱动，
1.0 协议漏传目标文本是其 0 命中的直接原因。

**仍未命中的任务（t2/t3/t4/t6/t7/t8）分层归因：**
- t6：锚点 A0AAC9SM19（AspX）已被 UniProtKB 删除——**数据卫生问题，
  与版本无关**（任何版本任何搜索模式都不可能返回已删除条目）。需找到
  后继 accession 更新 anchors.json，t6 discovery 评分在此之前不可解。
- t7/t8：锚点 BtnX（A8LT50）/ Q7SIG1 是 TrEMBL 条目。correct_cage 的
  TrEMBL 回退（`_uniprot_search_adaptive`）在本轮 **从未触发**——
  metrics.csv 的 trEMBL_anchor_in_pool 全部为空，因为各任务 Swiss-Prot
  查询非空（回退条件不满足）。keyword/text 通道也没有捞到这两个条目。
  即：fallback 是"能力"而非"保证"，reachable 变体按能力计入是对的，
  实证以 trEMBL_anchor_in_pool 为准的机制按设计工作。
- t2/t3/t4：CLAIRE EC 预测仍不准 + 关键词通道未覆盖（indole/hydratase
  类底物关键词与 UniProt 字段匹配度低），0 命中与 1.0 协议归因一致。

**版本进步结论（1.1 协议下的观察）：**
1. 092eb55 相对旧版本是**真实检索改进**：主流程 Hit@10 0→0.25、
   PoolRecall 0→0.25、综合分 2.5→11.0，全部由 goal 关键词/文本通道驱动，
   与 CAGE 无关（cage_audit.csv 证明 CAGE 特征在各版本一致）。
2. 但改进**未覆盖 TrEMBL discovery 任务**（t6/t7/t8 全 0）：TrEMBL 回退
   触发条件过窄（仅 Swiss-Prot 空结果才回退），对"Swiss-Prot 有结果但
   锚点不在其中"的任务没有帮助——这是 092eb55 检索链下一个可改进点
   （建议：Swiss-Prot 结果不足/无关时也触发回退，或直接并行双空间检索）。
3. 旧版本在 1.1 协议下与 1.0 协议下指标逐项相同——**1.1 协议向后兼容
   1.0 历史快照，历史结论（§2-§5）对旧版本仍然成立**。
