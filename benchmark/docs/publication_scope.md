# 发布范围：ami-reproduction / benchmark

核对日期：2026-10-24。目标为 `https://github.com/Nonlotus-eater/ami-reproduction` 的 `benchmark/`，保留 fork 根 AMI 实现、README、许可和 attribution。抽样默认恢复报告 v1，与 main 的 `27b57e4` 所发布固定五轮一致。
本次采用 benchmark 范围，包含四方法评测及 AML HTTP 客户端；服务端和自定义方法研究入口单独提供。保留 AMI、Mem0、InvMem、ReFind；不发布 LightMem / MemMA / LLMLingua。

## 上传清单

| 内容 | 处理 | 原因 |
|---|---|---|
| `src/memory_benchmark/` | 上传公共框架与 AMI/Mem0/InvMem/ReFind adapter | 四方法统一注册；排除 LightMem 三个模块和旧四方法 pilot，导出时同步去掉对应注册和测试 |
| `configs/reproduction/` | 上传 | 固定五轮参数、每轮 ID、210 个来源文件 hash、已验证的有界 runtime |
| `configs/small_benchmark_v1.json` | 纳入发布副本 | 从完整公开来源重新抽样，同样采用历史最多 30、小类至少 2 的 v1 规则 |
| 通用 runtime、数据下载版本、人工 smoke 配置 | 上传 | 与当前默认协议、下载与离线验证有关 |
| `sources/`、三个兼容补丁 | 上传 | 原生评分器、taxonomy、版本和归属；导出包 baseline lock 精简为四方法 |
| `scripts/` | 现用入口按白名单上传 | 配置、下载、公开来源抽样、sampled/fixed prepare、五轮运行、真实 smoke、发布导出，以及分块恢复、supervisor 接管和请求重试接入 |
| `tests/`、`examples/` | 上传相关内容 | 测试按功能模块组织，包含并发接管、续跑及四方法复用检查；保留 AMI 角色补丁和原生适配器检查 |
| `samples/`、`docs/sample.md` | 上传 | seed 17 的 22 份完整历史、202 组 QA pairs、阅读索引及六类数据案例 |
| `.env.example`、`pyproject.toml`、发布版 `.gitignore` | 上传 | 凭据模板、公共依赖和入口 |
| README、功能与流程介绍、当前 handoff、评测契约、能力映射、发布范围、配置/数据 README | 上传 | 运行与解释结果所需的说明 |
| 分块恢复说明、请求重试上线说明 | 上传 | 分块恢复接入、重试配置及恢复行为 |
| `docs/reader_aml_update.md` | 上传 | AML Reader 预算、裁剪方式和升级生效说明 |
| 旧 99 题入口、旧抽样与批次补跑/补评脚本 | 已从 scripts 删除 | 抽样复用公共模块，固定复现使用已发布 ID，当前队列自带 resume/retry；旧脚本清理前在工作区外备份 |
| 参考 Word | 保留此前已发布的参考资料 | 仅供历史报告追溯；当前入口不依赖它 |
| 论文 Word、五轮题目长附录、分析、每日记录、旧抽样说明 | 本地保留 | 不是运行依赖，避免把研究笔记当部署指导 |
| `runs/`、数据库、日志、账本、进程状态和原始模型响应 | 本地保留 | 不作为源码包分发；有需要时独立整理结果摘要 |
| `baselines/`、虚拟环境、`models/`、模型缓存 | 不上传 | 按固定提交安装；不跑 LightMem 无需 LLMLingua，也无需其 GPU/压缩依赖 |
| `datasets/` 完整原始快照、旧 99 题约 209 MiB 分卷 | 本地保留 | 完整来源另行下载/传输；seed 17 的阅读样例由 `samples/` 单独提供 |
| 真实 `.env`、旧 `.git` | 不进入发布目录 | 当前仓库已跟踪真实凭据，且历史包含它；不能靠增加 ignore 后直接 push |

正式部署仍需按数据 README 取得完整公开来源。默认固定题集复现须满足其精确来源指纹；同规则重新抽样也不读取旧 runs、旧 99 题 prepared 或参考 Word。

## 生成可审阅发布副本

从原工作区运行，输出目录必须不存在：

```bash
python3 scripts/export_publication.py --profile benchmark --output runs/publication-candidate-new/benchmark
```

工具按白名单复制当前工作区文件（包含尚未提交的新代码），不读取旧 Git 对象、不修改索引或 remote、不 commit/push。
`--profile benchmark` 包含四方法、审查第 1/3 项的执行隔离与配置冻结，以及第 6 项所需的 AML HTTP 客户端和 `http_memory` 依赖声明；`core` 排除该客户端，`full` 还包含 SDK 服务端和自定义研究后端。公开仓库的本次主发布范围使用 `benchmark`。
`PUBLICATION_MANIFEST.json` 列出输出文件和 SHA-256，记录四方法、生成变换和字节数。导出时针对本地长凭据值检查内容，只报告是否通过，不输出密钥。

它保留当前 `configs/runtime.json` 的 AML Reader 默认值（输入 117760、输出 8192、安全余量 2048、top_k=100），另附四方法历史实验的 `configs/reproduction/runtime.json`，不把两者合并成同一实验。
原生方法从固定版本独立获取，不把整个第三方 checkout 放进 fork。InvMem/ReFind 适配器位于 `src/memory_benchmark/methods/`，桥接位于 `src/memory_benchmark/runtime/native_bridge/`；适配器、桥接和补丁均在发布白名单中，不依赖历史 runs。

之后应在目标 fork 的独立本地 clone/分支中，把生成的 `benchmark/` 复制进去，review `git diff`，再按用户后续指令提交、push。
不要把原 Benchmark 仓库的 main/历史直接推到目标 fork，也不要修改本仓库 origin 来代替导出。

2026-10-17 的发布验证曾执行私有分支推送、重新 clone、全新依赖环境和四方法真实 API 检查。这是历史记录，不是后续修改自动推送或调用 API 的授权；新的发布须按用户当次要求执行。

真实配置检查使用 `scripts/release_smoke.py`，每方法处理两个完整人工历史、三道问题，包含原生记忆、检索、共同 Answer 和 Judge。它会产生 API 费用，不能作为六数据集正式评测成绩。

## 验证与限制

验证应在导出目录执行公共 tests 和公开来源抽样入口，确认移除历史脚本、Word 和旧 runs 后仍可抽样并衔接 prepare。默认固定五轮须核对其逐题 ID、1,003 题次和 112 个历史任务。
源码/接口检查通过与“全新服务器重新安装所有依赖”和“重新运行付费评测”分别记录。

当前 InvMem/ReFind adapter 仍支持显式研究配置的 null top_k；AML 默认配置固定为 100。运行配置变化需重新冻结和验证，不能用旧成绩证明新条件已完成评测。
