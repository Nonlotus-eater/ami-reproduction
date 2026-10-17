# Seed 17 数据样例

`seed-17/` 保存系列测试中 seed 17 实际使用的 22 份历史和全部 202 组 QA pairs。历史沿用原有内容和顺序，QA pairs 将同一道题的 Query 与 Target 放在一条记录中，方便一起阅读。

从 [数据说明与案例](../docs/sample.md) 开始阅读，或打开 [index.json](seed-17/index.json) 查找某个数据集、历史 ID 和 QA ID。

```text
seed-17/
├── index.json
└── <benchmark>/<case>/
    ├── history.json
    └── qa_pairs.jsonl
```

| 数据集 | 实际历史 | QA pairs |
|---|---:|---:|
| LoCoMo-Refined | 2 | 94 |
| LongMemEval-S | 6 | 6 |
| CLBench | 8 | 8 |
| PersonaMem v1 | 2 | 2 |
| PersonaMem v2 | 2 | 55 |
| BEAM 100K | 2 | 37 |
| 合计 | 22 | 202 |

数据来自五轮 v3 的 seed 17；题目 ID 与 `configs/reproduction/seed-17.json` 一致。`index.json` 给出每份历史及其 QA 文件的相对路径、会话数、消息数和题目 ID。
