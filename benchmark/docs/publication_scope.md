# 发布范围：ami-reproduction / benchmark

核对日期：2026-10-17。目标为 `https://github.com/Nonlotus-eater/ami-reproduction`，建议新增 `benchmark/`，不覆盖 fork 根 AMI 实现、README、许可和 attribution。
本次保留 AMI、Mem0、InvMem、ReFind；不发布 LightMem / MemMA / LLMLingua。

## 上传清单

| 内容 | 处理 | 原因 |
|---|---|---|
| `src/memory_benchmark/` | 上传公共框架与 AMI/Mem0 adapter | 排除 LightMem 三个模块和旧四方法 pilot；导出时同步去掉对应注册和测试 |
| `integrations/` | 上传 | 从历史运行目录提取 InvMem/ReFind adapter 和独立 worker/队列桥接，否则直接忽略 runs 会漏掉方法 |
| `configs/reproduction/` | 上传 | 固定五轮参数、每轮 ID、210 个来源文件 hash、已验证的有界 runtime |
| 通用 runtime、数据下载版本、人工 smoke 配置 | 上传 | 与当前默认协议、下载与离线验证有关 |
| `sources/`、三个兼容补丁 | 上传 | 原生评分器、taxonomy、版本和归属；导出包 baseline lock 精简为四方法 |
| `scripts/` | 按白名单上传 | 配置、下载、固定五轮 prepare、抽样算法、发布导出；不含本机补评和临时控制器 |
| `tests/`、`examples/` | 上传相关内容 | 公共逻辑及人工离线验证；保留 AMI 角色补丁检查 |
| `.env.example`、`pyproject.toml`、发布版 `.gitignore` | 上传 | 凭据模板、公共依赖和入口 |
| README、当前 handoff、评测契约、能力映射、发布范围、配置/数据 README | 上传 | 运行与解释结果必需的少量说明 |
| `docs/references/AMI_Benchmark_99_reference.docx` | 保留这一例外 | 旧抽样脚本读取表三；运行固定五轮不依赖它 |
| 论文 Word、五轮题目长附录、分析、每日记录、旧抽样说明 | 本地保留 | 不是运行依赖，避免把研究笔记当部署指导 |
| `runs/`、数据库、日志、账本、进程状态和原始模型响应 | 本地保留 | 不作为源码包分发；有需要时独立整理结果摘要 |
| `baselines/`、虚拟环境、`models/`、模型缓存 | 不上传 | 按固定提交安装；不跑 LightMem 无需 LLMLingua，也无需其 GPU/压缩依赖 |
| `datasets/` 原始数据、旧 99 题约 209 MiB 分卷 | 不进入 Git | 固定版本另行下载/传输，以 SHA-256 核验 |
| 真实 `.env`、旧 `.git` | 不进入发布目录 | 当前仓库已跟踪真实凭据，且历史包含它；不能靠增加 ignore 后直接 push |

“原始数据不进 Git”不代表可省略数据交付：固定五轮部署需取得全部来源文件，并通过指纹核验。源码包可离线验证人工例子；正式五轮需按数据 README 准备数据。

## 生成可审阅发布副本

从原工作区运行，输出目录必须不存在：

```bash
python3 scripts/export_publication.py --output runs/publication-candidate-new/benchmark
```

工具按白名单复制当前工作区文件（包含尚未提交的新代码），不读取旧 Git 对象、不修改索引或 remote、不 commit/push。
`PUBLICATION_MANIFEST.json` 列出输出文件和 SHA-256，记录四方法、生成变换和字节数。导出时针对本地长凭据值检查内容，只报告是否通过，不输出密钥。

它保留当前 `configs/runtime.json` 的无截断默认值，另附四方法已验证的 `configs/reproduction/runtime.json`，不把两者合并成同一实验。
原生方法从固定版本独立获取，不把整个第三方 checkout 放进 fork。InvMem/ReFind 桥接、adapter 和补丁已移到发布白名单中，不依赖历史 runs。

之后应在目标 fork 的独立本地 clone/分支中，把生成的 `benchmark/` 复制进去，review `git diff`，再按用户后续指令提交、push。
不要把原 Benchmark 仓库的 main/历史直接推到目标 fork，也不要修改本仓库 origin 来代替导出。

当前验证流程按用户要求为：先把源码包推送到私有 `Agent-Memory-Benchmark` 仓库的独立干净发布分支，重新从远端 clone 到临时目录，创建全新依赖环境并按交接文档配置，再运行四方法真实 API 最小链路。测试通过后，将同一源码包复制到公开 fork 的 `benchmark/`，以正常追加提交发布。最后删除本次新建的临时 clone、环境和测试产物；原工作区和历史实验保留。

真实配置检查使用 `scripts/release_smoke.py`，每方法处理两个完整人工历史、三道问题，包含原生记忆、检索、共同 Answer 和 Judge。它会产生 API 费用，不能作为六数据集正式评测成绩。

## 验证与限制

验证应在导出的目录执行公共 tests、离线 demo、两种 native adapter 离线检查，并使用现有原始来源准备五轮，确认 1,003 题次和 112 个历史任务。
源码/接口检查通过与“全新服务器重新安装所有依赖”和“重新运行付费评测”分别记录。

当前默认 `top_k=null` 尚不适用于固定 InvMem/ReFind HTTP schema；桥接会明确拒绝。四方法部署配置使用已验证的整数 100。统一无截断四方法协议需要独立适配和实测，不能用旧成绩证明。
