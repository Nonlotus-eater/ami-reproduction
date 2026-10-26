# 发布范围：当前比例抽样版本

目标：Nonlotus-eater/ami-reproduction 的 benchmark/；保留 fork 根 AMI 实现、README、许可和作者归属。本次发布已于 2026-10-26 通过用户审核。

## 当前保留

- proportional-200-v1 抽样策略、五 seed 清单、完整选中输入及比例表。
- run_parallel.py、parallel_runner/、配置生成、原始数据下载、当前抽样和准备工具。
- 四方法公共 Reader/评分/输入/隔离代码、固定来源与补丁、相关测试、AML HTTP 客户端。
- 当前部署/运行/评测说明、旧版本下载链接、公共 tokenizer 词表。

## 从新版发布目录移除

- configs/reproduction/、configs/small_benchmark_v1.json、samples/seed-17/ 及旧样例索引。
- run_five_seed_experiments.py、sample_small_benchmark.py、旧队列的 checkpoint/retry/proxy 升级与接管脚本、旧 release_smoke.py。
- 历史多轮合并工具 merge_experiment_repeats.py。
- 仅用于这些旧入口的测试与旧部署文档、旧 Word 样例。

底层公共准备、评分、Add checkpoint 和 HTTP 重试组件仍服务当前流程；移除旧脚本不意味着关闭新流程的断点恢复或自动重试。共享组件的测试保留，新并发和新抽样另有验证。

## 排除项

不分发 derived 脚本/方法/研究控制器，SDK 服务端/研究 backend，历史实验结果/日志/数据库，真实 .env，旧 Git 历史，虚拟环境、原生 checkout 及模型缓存。benchmark profile 保留 HTTP 客户端；core 排除 HTTP 客户端；full 是单独的研究分发选项，本轮不用。

旧版本固定为 6ab98adeacd50dc934772a3c64f630b3709f9a24，浏览和 ZIP 下载见 [旧版本](legacy_versions.md)。不删除该历史提交，也不清理本地旧实验依赖。

## 审核与导出

```bash
python scripts/export_publication.py --profile benchmark --output runs/new-candidate/benchmark
```

导出以白名单隔离旧内容，记录每文件 SHA-256 并扫描已知凭据值。不会改变本机 Git 索引/remote 或执行 push。后续获批后，在目标 fork 的独立 checkout 中同步审核副本，并按审核过的删除清单移除旧文件；仅复制新增文件会遗留旧脚本，因此不算完成上线。

当前工作区 origin 与目标 fork 不同，且含不属于发布范围的研究修改；不能把工作区整体或旧 Git 历史直接推送。

金额、任务/请求并发、RPM/TPM 默认不限；ReFind 默认去重，BEAM 转义修复纳入。具体默认值、行为边界与本轮修复在 README 中完整说明。

## 公开隐私测试数据与凭据检查

PersonaMem-v2 的两道 sensitive_info 题包含外形类似 API key 的生成文本；[作者的数据说明](https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2)明确这些隐私信息由模型生成，不使用真实世界输入。本次核对了固定版本 ed956dea41521fc4499acbc63f966e0fd3c053ba 的 benchmark.csv 第 381、1852 条（从 0 开始），与所选评分目标一致。

凭据检查只对这两份文件的精确路径和完整 SHA-256 匹配处理通用 key 外形误报；文件改动或换路径会继续拦截，任何已知本地/历史真实凭据及私有 env 文件仍优先拦截。测试题与原始输入字节保持不变，检查输出不展示 token。
