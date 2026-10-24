# Agent Memory Benchmark

六个公开数据版本的统一评测框架：LoCoMo-Refined、LongMemEval-S、CLBench、PersonaMem v1/v2、BEAM。支持 **AMI、Mem0、InvMem、ReFind**，各方法原生构建记忆与检索，共用作答、评分和汇总流程。

本轮新增统一并发、记忆写入分段与长文本处理、评分与请求诊断、四方法接入整理、公开数据选题与运行配置。详见 [同步说明](docs/sync_20261021.md)。

2026-10-24 发布记忆 worker 隔离、原生配置冻结和 AML HTTP 重试；Reader 预算及模型直连一并核对。更新说明见 [发布说明](docs/request_retry_deployment.md#2026-10-24reader-与审查第-136-项更新说明)。

2026-10-22 发布请求重试与预算修复，保留已有错误诊断，并支持已有实验在下一次记忆 worker 启动时接入。重试配置与 Add 断点恢复行为见 [上线说明](docs/request_retry_deployment.md)。

2026-10-23 Reader 对齐 AML 公开预算与排名前缀规则，升级方式见[简短说明](docs/reader_aml_update.md)。

## 配置与数据

在 Benchmark 根目录使用 Python 3.11 安装公共依赖；四方法原生源码与环境按 [交接文档](docs/model_handoff_99.md) 单独准备。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[api,data]' 'numpy==2.2.6'
```

复制 `.env.example` 为 `.env` 并填写凭据，按 [数据说明](datasets/README.md) 准备来源。生成本机配置、调整 `configs/local/suite.json` 后重建固定五轮输入：

```bash
.venv/bin/python scripts/configure_release.py --output configs/local
.venv/bin/python scripts/prepare_five_seeds.py --output runs/prepared-five-seeds-new
```

默认按 `configs/reproduction/` 的固定清单恢复 seed 17–21，五轮分别 202 / 199 / 196 / 201 / 205 题，共 1,003 题次、791 道不同题目。四方法共用冻结输入，每轮独立建库。[seed 17 样例](samples/README.md) 包含完整的 22 份历史和 202 组 QA pairs。

重新抽样使用 `scripts/sample_small_benchmark.py`、`scripts/prepare_sampled_seeds.py` 及 `configs/small_benchmark_v1.json`。Reader 默认按 AML 公开规则运行：top_k=100，完整输入最多 117,760 tokens，输出最多 8,192 tokens，安全余量 2,048 tokens；超预算时按检索排名保留证据前缀，问题、选项、指令和归档检索结果保持完整。历史运行参数保存在 `configs/reproduction/runtime.json`，详见 [配置说明](configs/README.md)。

## 四方法五轮并发

```bash
.venv/bin/python scripts/run_five_seed_experiments.py \
  --prepared runs/prepared-five-seeds-new \
  --config configs/local \
  --output runs/four-methods-new \
  --parallel 8 --parallel-per-method 2
```

每个“方法 × seed”为独立队列，共 20 个。`--parallel` 控制总活动队列数，`--parallel-per-method` 控制单方法活动队列数；全部同时启动使用 `--parallel 20 --parallel-per-method 5`。队列并发数与实时 API 请求数不同，运行会调用真实模型 API。

同一组参数追加 `--resume` 可续跑，复用已保存的答案和有效评分。调度器依赖 Linux；接管活动队列、调整并发及输出说明见 [交接文档](docs/model_handoff_99.md)。

同份历史内的 Add 分块恢复可通过 runtime 的 `memory_checkpoint_policy: "add_chunks_sqlite_snapshot_v1"` 启用，支持四方法。每块成功后保存数据库快照，失败时从最近的完整分块继续；建库完成后的检索失败也会复用建库状态。配置、恢复边界与已有实验接入见 [分块恢复说明](docs/memory_checkpoint_recovery.md)。

### 单方法五并发

用 `--method` 选择一个方法，并将两个并发上限都设为 5，即可同时运行该方法的 seed 17–21。以 InvMem 为例：

```bash
.venv/bin/python scripts/run_five_seed_experiments.py \
  --prepared runs/prepared-five-seeds-new \
  --config configs/local \
  --output runs/invmem-five-seeds-new \
  --method invmem \
  --parallel 5 --parallel-per-method 5
```

`--method` 可选 `activememoryindex`（AMI）、`mem0`、`invmem`、`refind`；省略时运行全部四方法，也可重复指定以选择多个方法。输出和汇总只包含所选方法，每个 seed 使用独立记忆库。

续跑时保留相同的 `--method`、输入、输出和运行参数，追加 `--resume`；更换所选方法时使用新的输出目录。

## 代码与说明

- `src/memory_benchmark/methods/`：四方法适配器与公共工厂。
- `src/memory_benchmark/runtime/native_bridge/`：InvMem/ReFind 原生执行桥接。
- `src/memory_benchmark/pipeline/`：数据冻结、Add 分段、Reader 与评测流程。
- `scripts/`、`configs/`：调度、抽样、配置生成与发布导出。
- [功能流程](docs/benchmark_guide.md)、[评测契约](docs/evaluation_contract.md)、[发布范围](docs/publication_scope.md)。

## AML HTTP 记忆接口

安装 `.[api,data,http_memory]` 后，可使用 `aml_sdk.client.MemoryClient` 或 `aml_http` adapter 连接兼容 Add/Search 的记忆服务。客户端提供有界重试及响应校验；请求重试规则见 [上线说明](docs/request_retry_deployment.md)。
