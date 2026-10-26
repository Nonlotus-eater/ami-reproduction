# 数据来源与重建

运行固定五 seed 可直接使用 `samples/proportional-200-v1/prepared/`，无需全量来源。题目清单、来源指纹和比例表位于 `configs/proportional-200-v1/`。

只有重新抽样或重建输入时，才需完整六数据集母库。模板为 `configs/datasets/text_suite.json`，配置生成器写入本地 `configs/local/suite.json`。

```bash
python scripts/download_public_datasets.py --root datasets/raw
```

脚本按 [public_sources.json](../configs/datasets/public_sources.json) 下载 CLBench、BEAM、PersonaMem v1/v2 的固定作者版本。另行准备 LoCoMo-Refined 的 conversations.jsonl、questions.jsonl，以及 LongMemEval 的 longmemeval_s_cleaned.json。路径可在本地 suite 中修改。

从完整母库抽样并冻结：

```bash
python scripts/sample_proportional.py --suite configs/local/suite.json --output runs/new-sampling
python scripts/prepare_sampled_seeds.py --sampling runs/new-sampling \
  --suite configs/local/suite.json --runtime configs/local/runtime.json --output runs/new-prepared
```

按已发布 ID 重新构建：

```bash
python scripts/prepare_five_seeds.py --suite configs/local/suite.json \
  --runtime configs/local/runtime.json --output runs/fixed-prepared
```

两条流程均禁止联网调用模型，校验母库来源和题目关联。母库 10,649 题，五 seed 每份 200 题，历史数为 57/56/57/58/58，BEAM 仅选 100K split 的完整历史。这些是公开数据的本地抽样，不是 AML 私有冻结套件。

旧数据组成与旧脚本仅在 [旧版本下载](../docs/legacy_versions.md) 提供。
