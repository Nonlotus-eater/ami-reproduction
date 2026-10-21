# 公开来源抽样与固定题集的数据准备

Git 中提供源码、v1 抽样配额，以及与此前 GitHub 发布一致的固定五轮 ID 与来源指纹（仓库历史名称 v3）。默认按固定清单重建；也支持从完整公开来源重新抽样。不需要旧 99 题分卷、旧运行结果或参考 Word。数据路径模板为 `configs/reproduction/suite.example.json`。

CLBench、BEAM、PersonaMem v1/v2 按作者固定版本下载：

```bash
.venv/bin/python scripts/download_public_datasets.py --root datasets/raw
```

版本清单见 [public_sources.json](../configs/datasets/public_sources.json)。脚本保存作者 snapshot 布局和 download manifest。
还需单独准备：

- `datasets/raw/LoCoMo-Refined/conversations.jsonl` 与 `questions.jsonl`（当前 text_only 策略）。
- `datasets/raw/LongMemEval/longmemeval_s_cleaned.json`，保留每题完整 haystack。

可使用其他本地目录，在生成的 `configs/local/suite.json` 中调整 `data_dir` / `data_file` / `source_manifest` / `source_snapshot`。
需要重新抽样时使用下面的离线入口，配额恢复为 seed 17–21、历史最多 30、可用小类至少 2、大类最多 40。历史按 seed 补足，不强制每个选中历史保留题目：

```bash
.venv/bin/python scripts/sample_small_benchmark.py \
  --suite configs/local/suite.json --config configs/small_benchmark_v1.json \
  --output runs/sampling-public-new
.venv/bin/python scripts/prepare_sampled_seeds.py \
  --sampling runs/sampling-public-new --suite configs/local/suite.json \
  --runtime configs/local/runtime.json --output runs/prepared-public-new
```

抽样与冻结都不调用模型。第一步记录本次来源 hash 和题目关联，第二步校验这些 hash 并生成完整历史、查询和评分目标。
通用抽样函数位于 `pipeline/source_sampling.py`，与历史审计脚本分离。

默认使用下面的 `prepare_five_seeds.py`，保证题目与此前 GitHub 发布版本精确一致。
`configs/reproduction/frozen_samples.json` 中的 210 项 SHA-256 约束这条固定题集复现流程。

```bash
.venv/bin/python scripts/prepare_five_seeds.py \
  --suite configs/local/suite.json --runtime configs/local/runtime.json \
  --output runs/prepared-five-seeds-new
```

该工具禁止联网，不调用 API，不依赖旧 runs。它按固定 ID 重建历史/查询/评分目标并冻结当前框架/runtime，核验五轮 202/199/196/201/205 题及 22/23/22/24/21 个实际历史。
原始下载数据、prepared 和运行结果都由 Git 忽略。它们是作者公开数据，不是 AML 私有冻结套件。

原工作区已有来源可复用，但应先过相同 hash 校验。旧 99 题迁移脚本已清理，历史分卷保留作追溯，不是部署入口。
