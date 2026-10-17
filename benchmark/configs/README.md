# 配置入口

发布支持 AMI、Mem0、InvMem、ReFind。固定五轮 v3 位于 `reproduction/`，逐轮 QA 为 202/199/196/201/205，共 1,003 题次、112 个历史任务/方法。

```bash
.venv/bin/python scripts/configure_release.py --output configs/local
```

生成四方法 `methods.json`、逐方法 `methods.<name>.json`、`suite.json`、`runtime.json` 和桥接 `embedding_policy.json`。
使用 fork 根 AMI 时可加 `--ami-root ..`；所有环境与数据路径均应按实际机器核对。脚本不读取或复制密钥，输出目录必须不存在。

- `runtime.json`：当前公共默认值，`top_k`、Reader 证据与完整输入上限均为 null，公共层不再额外截断。
- `reproduction/runtime.json`：四方法已验证的实验输入配置，top_k=100、Reader=4096、完整输入=119000、请求/worker 超时 180/86400 秒。配置生成器明确读取这一份。
- 两者 Answer `null` 均由网关解析为代码中的模型最大输出 16384，Judge=16384，记忆默认=2048，GPT-4o mini、o200k_base、temperature=0。
- 固定 InvMem/ReFind 原生 HTTP schema 不接受 null top_k；桥接拒绝它，不静默转换。AMI/Mem0 的无截断默认支持不代表四方法均已在此条件下运行。

`reproduction/seed-*.json` 是精确入选 ID；`frozen_samples.json` 保存来源 hash 和预期数量。`suite.example.json` 是可移植数据路径模板。
旧 `family20-*`、`reliability-*` 等目录是本地历史版本，不进入本次导出。

凭据仅填本地 `.env`：`OPENAI_BASE_URL` / `OPENAI_API_KEY` 为聊天，`AMI_EMBED_BASE_URL` / `AMI_EMBED_API_KEY` / `AMI_EMBED_MODEL` 为共享 embedding。维度当前 1024，batch 1–10。
ReFind 原生 BM25/agent 不调用 embedding。env 每行 `KEY=value`，不执行 shell 展开，HTTPS 根地址不加 `/chat/completions` 或 `/embeddings`。

队列读取 prepared 中冻结 runtime，修改配置文件不会更新旧 prepared；条件变化新建目录，不能改旧 hash 绕过校验。
部署、真实调用入口与历史条件差异见 [当前模型交接](../docs/model_handoff_99.md)。
