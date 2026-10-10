# LoCoMo 单 session / Qwen 冒烟测试

`locomo_session.py` 用外部 LoCoMo_refined 数据运行 AMI Add/Search，再由 Qwen
读取检索结果回答问题。它是本地研究入口，允许更换 LLM；academic 启动器的模型限制
仍然保留。核心 app 代码、上游提示词和检索算法未修改。

## 已完成的真实测试（2026-10-10）

- 数据：`/home/fengruizhe/Agent_Memory/datasets/LoCoMo_refined`。
- `conv-26` 的第 1 个 session，18 条文本消息，选择 2 道纯文本且所有标注证据都在
  本 session 的问题。其余 session 不写入。
- 复用迁移测试的 `memma/.env.qwen-api`，模型 `qwen3-8b`，非思考模式；
  Embedding 为 `text-embedding-v4`、1024 维，batch 10。
- 人名作为消息内容前缀保留；数据未指定时区，session 时间按 UTC 解析。
- 原始消息与抽取事实同时写入，显式更新开关沿用公开 release 模板。
- 使用 FastAPI TestClient 执行真实路由和 startup，真实网络仅用于模型 API；
  没有启动监听端口，没有验证反向代理或外部 HTTP 网络部署。
- Add HTTP 200；两个 Search 均成功，各返回 top-10。SQLite 实际持久化
  raw=18、fact=18；向量矩阵为 36×1024，每行向量 BLOB 为 4096 字节。
- 未检测到显式更新，updates=0；两个问题的原始证据召回率均为 1.0。
- 真实调用：LLM 6 次、Embedding 7 次（包含启动预热），无重试。
- 按北京公开价格估算费用 ¥0.0039465；免费额度、缓存折扣及实际账单不纳入估算。
  价格来源：<https://help.aliyun.com/zh/model-studio/model-pricing>。

| 问题 | Qwen 回答结果 | 与参考答案对照 |
|---|---|---|
| Caroline 何时参加 LGBTQ support group？ | 2023-05-08 | 错误：参考为 2023-05-07；证据说的是 session 前一天 |
| Caroline 可能选择哪些教育方向？ | counseling or mental health fields | 语义符合参考答案；此判断为人工对照 |

工程链路成功不等于两题全部回答正确。未调用正式 Qwen3-14B judge，未报告正式
benchmark 准确率。词级 F1 分别为 0.25、0.5，这个指标受答案措辞和长度影响。
单 session 子集也不能代表完整长对话评测。

本次完整文件保存在 gitignored 的 `artifacts/locomo-qwen-once/`：
`plan.json`、`report.json`、`predictions.jsonl`、每题检索结果、`memory.sqlite3`。
参考答案仅在生成预测后用于本地评分，不进入抽取、检索改写或回答提示词。

## 使用入口

默认仅检查数据，不读取凭据或调用模型：

```bash
.venv/bin/python -m reproduction.locomo_session
```

离线运行真实 app 路由，SDK 使用 fixture，并禁止出站连接：

```bash
.venv/bin/python -m reproduction.locomo_session --fake --output artifacts/locomo-fake-new
.venv/bin/python -m unittest reproduction.test_locomo_session -v
```

真实入口要求明确 `--execute --env-file`。配置字段见 `qwen.env.example`，其价格单位
为人民币每百万 Token。本次私密 `.env.qwen` 已准备，权限 600，并被 Git 忽略。

```bash
.venv/bin/python -m reproduction.locomo_session \
  --execute --env-file .env.qwen --max-cny 1 \
  --output artifacts/locomo-qwen-once
```

以上真实命令已经执行，目录内的 attempt.lock 会阻止重复执行。新的付费尝试需有对应
授权和新的输出目录。入口逐请求预留保守 token 费用，关闭思考、重试和重定向，限制
LLM/Embedding 请求数，并在首次 provider 错误后禁止后续请求。费用保护基于配置价格
和标准 Qwen token 估计，不是账户级的账单额度设置。

## 验证范围

新入口的 7 个离线保护/数据筛选检查通过；已有 native embedding 与 real-minimal
保护测试共 15 项通过。新增 fake 单 session 路由检查通过，随后完成上述真实调用。
结果来自 AMI 上游实现和本仓库工程适配，不声明新的记忆算法。
