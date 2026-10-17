# 五轮固定题集的数据准备

Git 中提供源码、题目 ID 与来源指纹，不提供原始数据或旧 99 题分卷。数据路径模板为 `configs/reproduction/suite.example.json`。

CLBench、BEAM、PersonaMem v1/v2 按作者固定版本下载：

```bash
.venv/bin/python scripts/download_public_datasets.py --root datasets/raw
```

版本清单见 [public_sources.json](../configs/datasets/public_sources.json)。脚本保存作者 snapshot 布局和 download manifest。
还需单独准备：

- `datasets/raw/LoCoMo-Refined/conversations.jsonl` 与 `questions.jsonl`（当前 text_only 策略）。
- `datasets/raw/LongMemEval/longmemeval_s_cleaned.json`，保留每题完整 haystack。

可使用其他本地目录，在生成的 `configs/local/suite.json` 中调整 `data_dir` / `data_file` / `source_manifest` / `source_snapshot`。
`configs/reproduction/frozen_samples.json` 给出 210 个实际使用来源文件的 SHA-256；同名文件或任意更新版不能替代精确来源。

```bash
.venv/bin/python scripts/prepare_five_seeds.py \
  --suite configs/local/suite.json --runtime configs/local/runtime.json \
  --output runs/prepared-five-seeds-new
```

该工具禁止联网，不调用 API，不依赖旧 runs。它按固定 ID 重建历史/查询/评分目标并冻结当前框架/runtime，核验五轮 202/199/196/201/205 题及 22/23/22/24/21 个实际历史。
原始下载数据、prepared 和运行结果都由 Git 忽略。它们是作者公开数据，不是 AML 私有冻结套件。

原工作区已有来源可复用，但应先过相同 hash 校验。旧 99 题迁移脚本和分卷留在原工作区，不是本次新部署入口。
