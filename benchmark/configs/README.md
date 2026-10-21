# 配置入口

默认使用报告中的 v1 策略（仓库历史名称 v3），与 GitHub `ami-reproduction/main` 已发布的固定五轮一致：seed 17–21、每轮历史最多 30 段、可用小类至少 2 题、大类最多 40 题、总题数至少 91。BEAM 使用完整 100K 分片。

先按数据集抽取 2 / 7 / 4 / 2 / 2 / 2 份初始历史（LoCoMo / LME / CLBench / PersonaMem v1 / v2 / BEAM），再按 seed 补足小类覆盖和总题量。没有历史数量下限，不按覆盖增益优先补历史，也不强制每个选中历史保留题目。

## 与已发布题集精确一致

`reproduction/seed-*.json` 保存固定 ID；`frozen_samples.json` 保存 210 项来源指纹。配置本机来源路径后，按固定清单冻结：

```bash
.venv/bin/python scripts/configure_release.py --output configs/local
.venv/bin/python scripts/prepare_five_seeds.py --output runs/prepared-five-seeds-new
```

五轮分别为 202 / 199 / 196 / 201 / 205 题、22 / 23 / 22 / 24 / 21 个实际历史，合计 1,003 题次、791 道不同题目、112 个历史任务/方法。固定 ID 和来源指纹保持原样，不依赖旧 runs 或 Word。

## 从完整公开来源重新抽样

`small_benchmark_v1.json` 也恢复为上述 v1 规则；通用抽样函数位于 `pipeline/source_sampling.py`。使用不带预选 question_ids 的本机 suite：

```bash
.venv/bin/python scripts/sample_small_benchmark.py --suite configs/local/suite.json --config configs/small_benchmark_v1.json --output runs/sampling-public-v1-new
.venv/bin/python scripts/prepare_sampled_seeds.py --sampling runs/sampling-public-v1-new --suite configs/local/suite.json --runtime configs/local/runtime.json --output runs/prepared-public-v1-new
```

抽样、冻结均不调用模型；来源和清单写入 hash，prepare 必须使用同一 suite，输出目录必须不存在。相同来源和种子可重放；来源改变时，新抽样结果以本次清单为准。需要与 GitHub 已发布题集精确一致时，使用上一节的固定清单入口。

## 运行配置

配置生成器输出四方法 `methods.json`、逐方法 JSON、`suite.json`、`runtime.json` 和桥接 `embedding_policy.json`。可用 `--ami-root ..` 指向 fork 根 AMI；脚本不读取或复制密钥。

抽样策略回改不改动 LLM 运行条件：

- `configs/runtime.json`：公共模板，top_k、Reader 证据、完整输入上限为 null。
- `configs/reproduction/runtime.json`：四方法已验证的有界配置，top_k=100、Reader=4096、完整输入=119000、请求/worker 超时 180/86400 秒。生成器采用这一份。
- InvMem/ReFind adapter 对 null top_k 使用当前作用域的实际记忆数量；ReFind 保留原生 agent 策略。

队列读取 prepared 中冻结的 runtime；修改配置不会更新旧 prepared，条件变化须新建目录。凭据仅填本地 `.env`，详见 [当前模型交接](../docs/model_handoff_99.md)。

## 历史与入口维护

`family40-history40-50-leaf8-five-seeds-v4/` 及其历史结果仅供追溯，不作为默认部署入口。`reproduction/sampling.json` 中的 historical_sampler 记录原始生成工具；该旧脚本已清理，精确复现由固定 ID 与来源指纹保证。

`scripts/` 保留配置、下载、公开来源抽样、固定/新抽样冻结、四方法运行、真实 smoke、发布导出八个入口。旧 99 题恢复及批次专用补跑/补评脚本已删除；当前队列保留 resume/retry 能力。`tests/` 按聚合、数据、流水线、队列、抽样、Reader、运行时和 CLBench 评分组织，保留现用逻辑的检查。
