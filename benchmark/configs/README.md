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

默认部署从 2026-10-23 起按 [AML Runtime Rules](https://agentmemories.ai/api-guide) 的公开 Reader 预算运行：

| 配置项 | 默认值 | 含义 |
|---|---|---|
| `top_k` | `100` | Search 最多返回 100 条；超量判契约错误 |
| `answer_input_token_limit` | `117760` | 包括问题、选项、指令、证据及消息封装的完整输入预算 |
| `answer_max_tokens` | `8192` | Reader 传给模型调用的 `max_tokens` |
| `answer_safety_margin_tokens` | `2048` | 128,000 上下文窗口的安全余量 |
| `answer_context_policy` | `aml_ranked_prefix_v1` | 超预算时只保留按检索排名排列的证据文本前缀 |
| `answer_output_policy` | `fixed_aml_budget` | 固定输出额度，不再按剩余窗口扩展 |
| `reader_token_budget` | `null` | 不另设证据额度；证据与其他输入共同使用完整输入预算 |

`configs/runtime.json`、各非 reproduction 的 runtime 模板以及本机 `configs/local/runtime.json` 已同步上述设置。配置生成器也继承全部 Reader 参数，生成的请求/worker 超时仍为 180/86400 秒；这些是本地模型调用与 worker 超时，不等同于官网 Add/Search 接口的单次请求时限。

证据裁剪按完整提示词计数，不会跳过放不下的高排名证据后再装入低排名记录；最后一条可只保留文本前缀。原始检索结果保留用于审计，Answer 记录包含完整/部分保留及省略的记忆 ID。问题、选项、指令本身已超预算时明确报错，不裁剪这些字段，也不调用模型。

`configs/reproduction/runtime.json` 仅保存历史实验参数（top_k=100、Reader=4096、完整输入=119000），不作为新运行默认值。旧 `model_window_v1/v2` 分段摘要策略保留用于显式历史/研究配置；`aml_ranked_prefix_v1` 不调用摘要模型。启动时会校验新策略的预算组合，避免配置漂移。

队列读取 prepared 中冻结的 runtime；修改配置不会更新旧 prepared，条件变化须新建目录并重新 prepare，不能直接续跑旧实验来应用新预算。凭据仅填本地 `.env`，详见 [当前模型交接](../docs/model_handoff_99.md)。

请求重试需要在上述 runtime 中设置 `"max_retries": 10`，并在 runtime 的 `env_file` 所指向的本地 `.env` 中设置 `AMI_ALLOW_RETRIES=1`。两份公共 runtime 模板已设置为 10；配置生成器会继承。10 表示首次请求失败后最多再试 10 次；OpenAI SDK 的 `max_retries` 保持 0，由 transport 统一执行和计账。已有本机配置不会自动被覆盖；功能与配置概览见 [上线说明](../docs/request_retry_deployment.md)。

## 历史与入口维护

`family40-history40-50-leaf8-five-seeds-v4/` 及其历史结果仅供追溯，不作为默认部署入口。`reproduction/sampling.json` 中的 historical_sampler 记录原始生成工具；该旧脚本已清理，精确复现由固定 ID 与来源指纹保证。

`scripts/` 保留配置、下载、公开来源抽样、固定/新抽样冻结、四方法运行、真实 smoke、发布导出，以及已有实验的分块恢复与请求重试接入入口。旧 99 题恢复及批次专用补跑/补评脚本已删除；当前队列保留 resume/retry 能力。`tests/` 按聚合、数据、流水线、队列、抽样、Reader、运行时和 CLBench 评分组织，保留现用逻辑的检查。
