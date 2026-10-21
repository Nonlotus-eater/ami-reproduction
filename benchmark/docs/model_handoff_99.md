# Benchmark 模型交接：v1 抽样与四方法部署

更新日期：2026-10-21（Asia/Shanghai）。保留旧文件名以兼容链接；默认恢复此前 GitHub 发布的固定五轮，也支持同一 v1 规则的公开来源重新抽样。
部署前先确认源码包含下面的新入口和配额文件；旧发布包可能仍使用历史入口，不能仅凭同名交接文档判断版本一致。

## 1. 当前入口与数据依赖

默认入口为 `scripts/prepare_five_seeds.py`，按 `configs/reproduction/` 的固定 ID 与来源指纹重建，与 GitHub `ami-reproduction/main`（核对提交 `27b57e4`）的已发布题集一致。
需要重新抽样时使用 `scripts/sample_small_benchmark.py` 与 `configs/small_benchmark_v1.json`，直接读取完整公开来源及本机 `configs/local/suite.json`。通用索引与抽样函数位于 `src/memory_benchmark/pipeline/source_sampling.py`。
两条流程均不读取旧 99 题 prepared、旧运行结果或参考 Word；依赖这些历史材料的旧抽样脚本已清理。

默认配额恢复报告 v1（仓库历史名称 v3）：seed 17–21，每轮完整历史最多 30、可用小类至少 2 题、大类最多 40 题、至少 91 题；BEAM 使用完整 100K 分片。
LoCoMo / LME / CLBench / PersonaMem v1 / v2 / BEAM 初始历史数为 2 / 7 / 4 / 2 / 2 / 2，再按 seed 补足小类覆盖和总题量。没有 40 段下限，不采用覆盖增益优先，不强制每个选中历史保留题目。
候选小类以代码中的已验证映射为准；A2/B2/B3 无映射，不补题或补零。轮内不重复，轮间允许重合，抽样不使用 gold 或方法得分。

已发布固定五轮为 202/199/196/201/205 题、22/23/22/24/21 份实际历史，合计 1,003 题次、791 道不同题目、112 个历史任务/方法。
40–50 / 8 的配置及结果保留作历史追溯；新实验以本次抽样清单及 prepared manifest 为依据。

支持 AMI、Mem0、InvMem、ReFind；四方法共用同一批冻结输入，各方法、seed、history 独立建库。
先读 README、配置说明、评测契约；执行逻辑在 `pipeline/`、`runtime/` 和方法 adapter 中。
四方法适配器统一在 `src/memory_benchmark/methods/`；InvMem/ReFind 现有调度另读 `src/memory_benchmark/runtime/native_bridge/`。原生版本与补丁以 `sources/publication-baselines.lock.json` 为准。

## 2. 执行和评分边界

公共链路：公开来源抽样 → prepare → 原生 add/finalize/search → 共同 Answer → 原生评分 → aggregate。
记忆 worker 只读历史和查询；gold、rubric、正确选项和能力标签留在评测侧。一个历史的所有回答保存后，队列才打开评分目标。
AMI/Mem0 使用公共队列；InvMem/ReFind 先用方法侧桥接生成检索断点，再调用相同的 Answer/Judge 阶段。
四方法均已注册到公共方法工厂。现有五轮运行脚本仍将配置拆成逐方法 JSON，分别启动公共队列或原生桥接；方法的独立依赖环境保持原配置。

LoCoMo/LME 使用 judged accuracy；CLBench strict accuracy；PersonaMem v1 MCQ accuracy、v2 preference adherence；BEAM rubric mean。
CLBench Judge 协议读取本次冻结 runtime；最新 continuation 使用 indexed-status-v2，旧记录中的 v1 仅描述历史条件。
公共队列的 `local_overall_macro_benchmark` 是六数据集等权均值；五轮实验汇总另有七个能力大类等权的主指标，须注明所用字段。
未完成冻结题集时不能用成功子集替代完整 Overall；网络、模型或解析失败保持 unscored。

## 3. 抽样参数与运行条件

抽样配额和 LLM runtime 分别配置。实际 prepared/运行 manifest 的冻结 runtime 才是依据，修改配置文件不会改变旧运行。
`configure_release.py` 仍生成四方法已验证的有界 runtime（top_k=100、Reader=4096、完整输入=119000）。
`configs/runtime.json` 则为公共无额外检索/输入截断模板；需要使用它时，把所需字段写入本机 runtime 后重新 prepare，不能把两种条件混为一轮。
当前 InvMem/ReFind adapter 已支持 null top_k：根据本任务作用域中的记忆数量调用同一原生检索实现。

模型为 GPT-4o mini，tokenizer 为 o200k_base，Answer/Judge temperature=0。
AMI/Mem0/InvMem 共用配置的 embedding API；ReFind 保留原生 BM25/agent，不调用 embedding。
运行时间、provider 限流、上下文分块及 Judge 协议以本次实际 manifest 为准；旧成绩不能证明修改后的配置已经完成真实评测。

## 4. 源码、凭据和公共环境

建议放到目标 `ami-reproduction` fork 的 `benchmark/` 下，保留根 AMI 源码、README、LICENSE、ATTRIBUTION 和 upstream 记录。
以下命令从 Benchmark 根目录执行，不依赖用户名、Conda 路径或旧服务器目录。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[api,data]' 'numpy==2.2.6'
.venv/bin/python -m pip check
cp .env.example .env
chmod 600 .env
```

编辑 `.env` 填写聊天和 embedding endpoint/key，不打印正文、不写进 JSON 或提交。
原工作区 `.env` 已被 Git 跟踪且存在于旧历史，`.gitignore` 不能撤销跟踪。迁入 fork 使用发布导出目录，不能推送、合并或搬运旧 `.git`。

## 5. 原生方法安装

只用 lock 固定提交；原生源码不包含在发布包中，分别获取并创建环境。已有目录先检查版本及本地差异，避免覆盖。

```bash
mkdir -p baselines
git clone https://github.com/mem0ai/mem0.git baselines/mem0
git -C baselines/mem0 checkout 71fba8d46436f88569d600f81a55208c38ad30b5
git clone https://github.com/Nonlotus-eater/ami-reproduction.git baselines/ami-reproduction
git -C baselines/ami-reproduction checkout e545fdb10ce139bb19dc7c6f814ef853ed19cba0
git -C baselines/ami-reproduction apply ../../patches/ami-system-role.patch
git clone https://github.com/wenxiaof345-ctrl/vanilla-rag-memory.git baselines/InvMem
git -C baselines/InvMem checkout 31ab7bf9cfa3ee3c4f986e82f6e7a00b134ba8ca
git -C baselines/InvMem apply ../../patches/invmem-native-interface.patch
git clone https://github.com/imlrz/ReFind.git baselines/ReFind
git -C baselines/ReFind checkout a80175ca0eeb52a938d7cab7a602bc780de8a577
git -C baselines/ReFind apply ../../patches/refind-native-interface.patch

python3.11 -m venv baselines/mem0/.venv
baselines/mem0/.venv/bin/python -m pip install -e baselines/mem0 -e '.[api]' \
  'fastembed==0.7.4' 'openai==2.3.0' 'httpx==0.28.1' 'numpy==2.2.6'
python3.11 -m venv baselines/ami-reproduction/.venv
baselines/ami-reproduction/.venv/bin/python -m pip install \
  -r baselines/ami-reproduction/requirements-academic.lock.txt -e '.[api]'
python3.11 -m venv baselines/InvMem/.benchmark-env
baselines/InvMem/.benchmark-env/bin/python -m pip install -e '.[api]' \
  'numpy==2.2.6' 'faiss-cpu==1.15.1' 'fastapi==0.141.1' 'pydantic==2.13.4' 'httpx==0.28.1'
python3.11 -m venv baselines/ReFind/.benchmark-env
baselines/ReFind/.benchmark-env/bin/python -m pip install -e '.[api]' \
  'fastapi==0.141.1' 'pydantic==2.13.4' 'httpx==0.28.1'
```

可用 `--ami-root ..` 指向 fork 根 AMI，前提是确认其固定版本/兼容补丁；默认独立 clone 可保留根目录原状。
InvMem adapter 把其 `src/` 加入方法进程路径，注入共享 API 向量，未启用本地神经模型，不需安装上游 Torch/SentenceTransformer 全量可选依赖。ReFind 保留 agent/planner。
Mem0 的 FastEmbed 用于原生 BM25，不能与 LLMLingua 混淆；首次配置应准备 `models/fastembed` 缓存：

```bash
baselines/mem0/.venv/bin/python - <<'PY'
from pathlib import Path
from fastembed import SparseTextEmbedding
cache = Path('models/fastembed').resolve()
cache.mkdir(parents=True, exist_ok=True)
encoder = SparseTextEmbedding(model_name='Qdrant/bm25', cache_dir=str(cache))
assert len(next(encoder.embed(['Alice lives in Kyoto.'])).indices) > 0
print('BM25 cache verified')
PY
```

上述准备可能下载依赖/缓存，不调用付费模型。保留各原生仓库作者归属与许可。InvMem 固定提交未发现根 LICENSE，不为它额外宣称许可证；本次分发小型适配代码和补丁，原生实现单独获取。

## 6. 本机来源配置与冻结

```bash
.venv/bin/python scripts/configure_release.py --output configs/local
.venv/bin/python scripts/download_public_datasets.py --root datasets/raw
```

下载脚本覆盖 CLBench、BEAM、PersonaMem v1/v2 的固定作者版本。
LoCoMo-Refined 的 conversations/questions JSONL 和 LongMemEval-S 的 cleaned JSON 另行准备，见 `datasets/README.md`。
把完整来源的真实路径写入 `configs/local/suite.json`；它描述母库，不应带预选 question_ids。
旧数据曾存放在 migration-99 路径下，不代表新入口需要这个目录；完整公开来源可放在任意配置的本机目录。

默认按固定清单重建，与此前 GitHub 发布版本精确一致；这条流程校验 210 项来源指纹：

```bash
.venv/bin/python scripts/prepare_five_seeds.py \
  --suite configs/local/suite.json --runtime configs/local/runtime.json \
  --output runs/prepared-five-seeds-new
```

需要从完整公开来源重新抽样时，使用同一 v1 规则：

```bash
.venv/bin/python scripts/sample_small_benchmark.py \
  --suite configs/local/suite.json --config configs/small_benchmark_v1.json \
  --output runs/sampling-public-v1-new
.venv/bin/python scripts/prepare_sampled_seeds.py \
  --sampling runs/sampling-public-v1-new --suite configs/local/suite.json \
  --runtime configs/local/runtime.json --output runs/prepared-public-v1-new
```

抽样与冻结均离线且不调用 LLM。新抽样保存来源指纹、逐 seed 历史定位和题目清单；prepare 检查指纹并恢复完整历史、查询和独立评分目标。
必须使用相同的 suite，输出目录必须是新目录。服务器无需补传旧 99 题、旧运行结果或参考 Word。

## 7. 离线检查与真实执行

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m memory_benchmark demo --output runs/demo-new
baselines/InvMem/.benchmark-env/bin/python tests/native/test_adapters.py baselines/InvMem invmem
baselines/ReFind/.benchmark-env/bin/python tests/native/test_adapters.py baselines/ReFind refind
```

adapter 检查禁止联网，以替身验证原生 API、system 消息、时间、检索和用户隔离，不证明真实模型分数。
公共测试和现有方法环境通过不等同于全新服务器依赖安装验收。
2026-10-17 已另行完成远端新 clone、五套全新依赖环境和四方法真实 API 配置检查，范围与结果见 [配置验证记录](configuration_validation_20261017.md)。

获得真实配置测试授权后，可一次运行四方法的最小 API 链路：

```bash
.venv/bin/python scripts/release_smoke.py --output runs/real-api-config-new
```

它逐方法运行两个完整人工历史、三道 QA 的原生记忆、检索、Answer 和 Judge，检查 3/3 评分及非空检索；不运行正式五轮。失败日志保留在输出目录，不自动重试。

下面会调用真实 API，仅在用户要求评测时执行。每方法每轮用新输出目录；seed 17 示例，其余替换为 18–21：

```bash
# AMI；Mem0 改为 methods.mem0.json 和独立输出目录。
.venv/bin/python -m memory_benchmark.runtime.queue \
  --prepared runs/prepared-five-seeds-new/seed-17 \
  --methods-config configs/local/methods.activememoryindex.json \
  --output runs/ami-new/seed-17

# InvMem；ReFind 改为 methods.refind.json 和独立输出目录。
.venv/bin/python -m memory_benchmark.runtime.native_bridge.runner \
  --prepared runs/prepared-five-seeds-new/seed-17 \
  --methods-config configs/local/methods.invmem.json \
  --policy configs/local/embedding_policy.json \
  --output runs/invmem-new/seed-17
```

上述入口每次运行单个队列。四方法五轮可以统一交给固定调度脚本；以下命令会调用真实模型 API：

```bash
.venv/bin/python scripts/run_five_seed_experiments.py \
  --prepared runs/prepared-five-seeds-new \
  --config configs/local \
  --output runs/four-methods-new \
  --parallel 8 --parallel-per-method 2
```

脚本默认运行全部四方法；使用 `--method invmem` 可只运行 InvMem，重复 `--method` 可选择多个方法。配置目录须包含 `methods.json` 和所选方法的逐方法配置；InvMem/ReFind 还需要 `embedding_policy.json`。prepared 目录包含 seed 17–21。各方法共用冻结输入，各方法/seed 独立建库。`--parallel` 是总活动队列上限，`--parallel-per-method` 是单方法活动队列上限；20 个队列全部并发时设为 20 和 5，若只需总计 5 个活动队列，可设为 5 和 2。

单方法五并发使用 `--method invmem --parallel 5 --parallel-per-method 5`，并设置独立输出目录；其他方法名称为 `activememoryindex`、`mem0`、`refind`。报告只汇总所选方法，续跑须保持方法选择一致，更换方法须另建输出目录。完整示例见 [README](../README.md#单方法五并发)。

控制器通过独立子进程调用公共队列或原生桥接，队列内部依次处理历史和各题阶段。默认最多执行 3 次队列 pass，只继续失败或未完成阶段，保留已有答案和有效评分。`--passes` 不是模型 API 自动重试次数。`progress.json` 保存活动队列和逐轮状态，`summary.json`、`overall_comparison.csv` 同时保存七能力等权与六数据集等权统计，主报告使用七能力等权。

正常续跑使用相同 prepared/config/output、并发数和 passes，加 `--resume`。若原控制器已经退出而子队列仍在运行，再加 `--adopt-running` 可核对 PID 和启动标识后接管。调整并发时加 `--resume --resize-concurrency` 并传入新并发数；有活动子队列时同时加 `--adopt-running`。原控制器必须先退出，避免两个控制器争用同一输出；输入或方法代码变化须另建实验目录。初始计划保存在 `launch_plan.json`，并发变更追加到 `scheduling_changes.jsonl`。

`embedding_policy.json` 的 `{}` 表示不施加桥接节流；并发时可显式配置 `state_directory`、`minimum_interval_seconds=0.25`、`cooldown_seconds=30`，同 provider 的桥接队列共用状态目录。桥接不隐藏失败或自动重放请求。
两个入口均支持相同 prepared/config/output 加 `--resume`，失败任务另加 `--retry-failed`。先确认同目录无活动队列；输入、源码、provider identity 或 runtime 变化须另建运行，不能修改旧 hash 绕过检查。

## 8. 验收和历史结果

以本次 `prepared/summary.json` 和每个 seed manifest 的问题数为预期值，逐轮逐方法要求 expected=scored、complete=true、各数据集无缺项。
默认 v1 固定五轮每方法共 1,003 题次、112 个历史任务；重新抽样时须以实际冻结清单为准。

历史 40–50 / 8 实验结果位于 `runs/four-methods-history40-50-leaf8-20261019-v3-continuation/summary.json`。
这里的 v3-continuation 是运行恢复版本名，其题集仍为历史 v4，不代表恢复后的默认 v1 已重新评测。
运行结果、数据库和日志保存在本地，发布时另行整理经过核验的摘要与条件。

本文描述配置方式；实际评测、恢复或发布均按用户当次明确授权执行，文档本身不授权自动启动这些操作。
