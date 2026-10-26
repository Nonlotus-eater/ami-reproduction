# Agent Memory Benchmark

六个公开文本数据版本的统一评测：LoCoMo-Refined、LongMemEval-S、CLBench、PersonaMem v1/v2、BEAM。支持 **AMI、Mem0、InvMem、ReFind**，保留各方法原生记忆与检索，共用 Reader、评分及报告。

当前数据版本为 `proportional-200-v1`：seed 17–21，每个 200 题，共 **1,000 道不同题目、286 个历史任务**。当前目录只分发新抽样、新五 seed 和新并发脚本，不含 derived、旧数据样例、旧队列入口或历史多轮合并工具。

## 运行新五 seed

需要 Linux、Python 3.11、bubblewrap；公共依赖及四方法安装见 [部署说明](docs/deployment.md)。复制 `.env.example` 为本地 `.env` 并填写模型接口凭据，然后执行：

```bash
python scripts/configure_release.py --output configs/local
python scripts/run_parallel.py prepare --config configs/local/parallel.json
python scripts/run_parallel.py check --config configs/local/parallel.json
python scripts/run_parallel.py start --config configs/local/parallel.json
python scripts/run_parallel.py status --config configs/local/parallel.json
```

prepare/check 不调用模型；start 启动真实付费请求。完整所选历史、查询及独立评分目标包含在 `samples/proportional-200-v1/prepared/`，运行固定输入不依赖旧 runs 或全量原始数据下载。

**默认不设金额、历史任务、每方法任务、题目请求并发或接口 RPM/TPM 上限。** JSON 中这些限制均为 `null`，表示所有符合阶段依赖的任务都可同时启动，不再默认限制为 20/5。每份历史仍需完成 Add/Search 才能作答，作答完成后再评分；操作系统资源和供应商自身限额仍生效。费用会继续记账，估算单价由用户配置。

用户可自行填写任意正整数的并发限制，没有代码写死的最大值。例如下面是一份**用户主动限制**的配置，并非默认值：

```bash
python scripts/configure_release.py --output configs/local-limited \
  --history-workers 20 --per-method-workers 5 --query-workers 1 --budget-cny 50
```

接口限制在生成的 `parallel.json` 的 `providers.llm`、`providers.embedding` 中设置：`inflight`、`rpm`、`tpm` 接受正整数或 null。`memory_workers`、`reader_history_workers` 可分别限制阶段并发。自动降低接口并发默认关闭，可在设置有限接口并发后显式开启。配置在 prepare 时冻结，变更配置应另建运行目录。

## 抽样与报告

按 10,649 题母库的数据集及原生题型比例分配题数，通过五 seed 联合取整减小误差。复用完整历史以减少 Add，BEAM 每 seed 选两份 100K split 历史，不覆盖 500K/1M；100K 是分档名，不是精确 tokenizer 长度。六数据集五 seed 合计题数为 **81 / 47 / 178 / 55 / 470 / 169**。

能力类型沿用本地映射并另做比例验收：合并后大类、小类占比最大误差分别为 **0.0877、0.0664 个百分点**。母库未覆盖 A2/B2/B3，个别 seed 稀有小类也可无题。完整表格见 [数据集比例](configs/proportional-200-v1/dataset_distribution.csv)、[能力比例](configs/proportional-200-v1/capability_proportion_audit.csv)。

报告以七能力大类等权均分为主，同时提供六数据集等权均分、各数据集、能力大类与小类得分、五 seed 均值和样本标准差（ddof=1）。缺失成绩不填零，未覆盖小类记 NA，并给出有效 seed 数。输出包括 `report.md` 和 `summaries/{benchmarks,capabilities,capability_leaves}.csv`。

从全母库重新抽样使用 `scripts/sample_proportional.py`；重建固定 ID 使用 `scripts/prepare_five_seeds.py`。数据与运行命令见 [数据说明](datasets/README.md) 和 [运行说明](docs/proportional_parallel_release.md)。

## ReFind 默认去重与超限处理

默认 `refind_context_policy=deduplicate_observations_v1`，对 **全部 ReFind 题目**统一启用。规划过程中，同一原文块重复出现在观察中时保留首次原文，后续用引用表示，保留结果编号和 MATCH 标记。如果去重后仍超限，仅对观察正文取头尾片段，使完整规划提示词不超过 117,760 tokens；问题、系统指令、动作和结果头部不截断。记忆数据库及最终交给 Reader 的检索证据不改写。设为 null 可关闭。

这会改变 ReFind 的规划输入，可能改变后续检索与回答。此前实验仅最后 5 道失败题补跑采用去重，不能把那轮分数标为当前默认设置的完整实验结果。

## BEAM 评分 JSON 转义修复

BEAM Judge 有时在评分解释中写出 LaTeX，例如 `\geq`，却没有按 JSON 规则转义反斜杠，导致整份评分解析失败。现在只有原生解析器明确报告 `Invalid \\escape` 时，才将字符串中的非法反斜杠改成合法的字面量转义，再交给**同一个原生评分校验器**。

这项修复不改 rubric、分数或合法 JSON 转义，也不替模型补评分；其他语法错误继续报错。保存原始评分响应，并用 `judge_json_normalization` 记录是否修复及修复次数。

## 本轮集成与修复

- 新并发入口统一为 `scripts/run_parallel.py`，实现分阶段运行、停止、恢复、共享用量账本及细分报告；发布包移除旧入口和旧样例。
- 修复 Python 虚拟环境链接被解析成系统解释器的问题；启动从本次冻结源码加载，避免另一 editable checkout 提前污染导入。
- JSONL 按文件物理行解析，保留正文中的 Unicode 行分隔符，避免误拆记录。
- 用户配置的总并发和每方法并发跨记忆、Reader 两池合计执行；null 默认不限，显式填写时才限制。
- Add 分块、检索、答案、评分分别持久化；续跑保留成功结果；冻结并校验源码、环境、输入和参数，拒绝静默篡改。
- 记忆 worker 使用 bubblewrap 文件隔离，不挂载评分目标；修复进程超时处理及私有 provider 临时文件清理。
- 模型 HTTP 请求直接连接接口，不读取环境代理；SDK 不叠加重试。即使供应商用 429 包装上下文超限，也识别为永久错误，不进行无效请求重试。
- 默认 ReFind 观察去重及超限处理、BEAM JSON 非法转义修复，行为如上节所述。

新并发脚本仍保留独立的故障重试：默认每请求最多 8 次尝试、每历史任务最多 3 次尝试，均可配置。这是故障恢复次数，与“金额和并发默认不限”不同。

“旧队列重试增强”原本给旧运行器补上 Reader/评分请求的传输重试，并支持显式最多 100 次重试。例如已有答案保存成功、评分请求连接失败时，只重试评分请求，保留已有答案。它不改变 Reader token 预算，也不按分数高低重跑。旧队列升级脚本本轮随旧入口移除；新脚本自身的重试保留。

Reader 协议保持 top100、完整输入 117,760、输出 8,192、安全余量 2,048；金额和并发默认不限不改变这些模型上下文约束。评测与隔离边界见 [评测契约](docs/evaluation_contract.md)。

## 修复：隔离任务误判正常请求退出（2026-10-26）

此前，任务 B 在独立隔离环境里看不见仍在运行的任务 A，就可能误以为 A 已经退出，提前修改 A 的账本记录；即使模型随后返回 HTTP 200，A 也会因结算失败而报错。

现在每个请求在共享账本旁持有一把文件锁，从进入等待队列一直保留到结算和释放许可完成。其他任务通过这把锁判断请求是否仍在工作；进程异常退出时，系统自动释放锁。排队请求确认退出后取消未发送预留，已发出请求保留未知费用，防止漏记。停止后恢复也使用同样的机制。

新增真实 bubblewrap 隔离与本地 HTTP 回归：宿主侧和两个独立隔离 worker 双向交错请求，均返回 200 并正常结算；同时验证排队/在途进程突然退出后的回收、恢复时拒绝仍在运行的请求，以及重复结算仍被拒绝。

**升级已有运行：**旧运行目录已经冻结了旧 launcher，更新仓库后直接 resume 不会自动修好旧代码。请保留旧输出和账本，使用新的 `output`、`ledger_dir` 重新 prepare/check，再 start。新版会明确拒绝旧 PID 账本；已有 unknown 记录需要核对，不能直接改成成功。新运行期间请保留 SQLite 账本旁的 `.leases` 目录及锁文件。

## 下载旧版本

旧数据、旧抽样及旧队列脚本从当前发布目录移除，仍可在固定历史提交中查看和下载：

- [旧版 benchmark 目录](https://github.com/Nonlotus-eater/ami-reproduction/tree/6ab98adeacd50dc934772a3c64f630b3709f9a24/benchmark)
- [下载旧版本 ZIP](https://github.com/Nonlotus-eater/ami-reproduction/archive/6ab98adeacd50dc934772a3c64f630b3709f9a24.zip)，解压后进入 `benchmark/`
- 固定提交：`6ab98adeacd50dc934772a3c64f630b3709f9a24`

旧版包包含当时的五 seed ID/配置、seed 17 阅读样例和脚本；完整母库仍按旧版数据说明下载。不要把旧脚本、旧 Reader 参数与当前数据混合使用。详见 [旧版本位置](docs/legacy_versions.md)。

本次更新的完整范围见 [发布范围](docs/publication_scope.md)。

## AML HTTP 记忆接口

安装 `.[api,data,http_memory]` 后，可使用 `aml_sdk.client.MemoryClient` 或 `aml_http` adapter 连接兼容 Add/Search 的记忆服务。客户端提供有界重试及响应校验；实验请求及恢复规则见 [运行说明](docs/proportional_parallel_release.md)。
