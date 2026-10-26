# 200 题比例抽样与并发运行

当前版本仅分发 proportional-200-v1：seed 17–21，每 seed 200 题。旧数据、旧抽样和旧队列工具见 [旧版本下载](legacy_versions.md)，不混放在当前发布目录。

## 固定输入运行

按 [部署说明](deployment.md) 准备 Python 3.11、Linux bubblewrap 和四方法环境，填写本地 .env：

```bash
python scripts/configure_release.py --output configs/local
python scripts/run_parallel.py prepare --config configs/local/parallel.json
python scripts/run_parallel.py check --config configs/local/parallel.json
python scripts/run_parallel.py start --config configs/local/parallel.json
python scripts/run_parallel.py status --config configs/local/parallel.json
```

prepare/check 不调用模型；start 使用真实接口。相对路径按配置文件所在目录解释。输出和账本使用新目录，固定输入不依赖旧 runs。prepare 冻结并检查输入、框架、原生源码及 Python 环境。

## 默认不限与用户配置

| 配置 | 默认 null 的含义 |
| --- | --- |
| budget_cny | 不设金额上限，继续记录估算用量与费用 |
| total_history_workers | 全部历史任务均可启动，不设全局并发上限 |
| per_method_history_workers | 不设每方法历史任务并发上限 |
| memory_workers / reader_history_workers | 各阶段可执行全部已就绪任务 |
| query_workers | 同一历史内所有已就绪题目可并行作答/评分 |
| providers.*.inflight / rpm / tpm | 不额外限制模型接口并发、每分钟请求或 token 数 |

用户可填写正整数收窄任一并发/速率限制，没有内置 20/5 等最大值。金额预算接受正数。操作系统、方法原生内部参数、服务商限额和阶段依赖仍有效；null 不保证物理资源无限。自动并发降速默认关闭；显式开启且设置有限 LLM inflight 时才启用。

例如自选限制：`configure_release.py --output configs/local-limited --history-workers 20 --per-method-workers 5 --query-workers 1 --budget-cny 50`。接口限速及阶段并发在生成的 parallel.json 中设置。不要在 prepare 后修改冻结配置；变更条件使用新目录重新 prepare。

## 停止、恢复与故障重试

```bash
python scripts/run_parallel.py stop --config configs/local/parallel.json
python scripts/run_parallel.py resume --config configs/local/parallel.json --clear-user-stop
```

普通中断恢复使用 resume。成功 Add 块、Search、答案和评分持久化；不会按分数高低重试。已耗尽任务尝试次数且标为 failed 的历史，普通 resume 不会自动重置，需要先查原因再做有记录的恢复。

新 transport 默认每请求最多 8 次尝试、历史任务最多 3 次，SDK 自身重试为 0；只处理暂时连接、超时及适用的服务错误。上下文超限即使被包装为 429，也作为永久错误处理。旧 enable_request_retries.py 是给旧队列追加 Reader/评分重试的升级工具，本轮不分发；新调度器不依赖它。

HTTP 客户端直接连接配置的模型 endpoint，不读取环境代理。记忆 worker 使用 bubblewrap，只挂载当前历史/查询、对应记忆状态、冻结源码、Python 环境、tokenizer 词表和共享账本；评分目标不挂载。缺少隔离能力会拒绝启动。

## 抽样与报告

完整母库为 10,649 题。按数据集及数据集原生题型分配配额，五 seed 联合取整；共享完整历史，LoCoMo 与 BEAM 每 seed 各两份，BEAM 仅 100K split。能力比例按既有映射另行验收，不改变能力定义；A2/B2/B3 母库不覆盖。

重抽使用 sample_proportional.py，重建固定 ID 使用 prepare_five_seeds.py，命令见 [数据说明](../datasets/README.md)。这两种情况需要完整母库；运行已附输入不需要。

输出含逐题回答/评分、PROGRESS.json、report.md，以及 summaries/benchmarks.csv、capabilities.csv、capability_leaves.csv。主指标为七能力大类等权均分，同时保留六数据集等权均分；跨 seed 使用样本标准差。细分类无题记 NA、显示有效 seed 数，缺题不宣称完整成绩。合成离线测试不报告方法准确率。

## 显式行为变化

ReFind 对所有题默认启用 deduplicate_observations_v1：重复规划观察去重，必要时截取观察正文头尾，使完整提示词不超过 117,760 tokens；问题、指令、动作、结果编号及最终检索证据保留。设 null 关闭。旧实验仅最后 5 道补跑题使用去重，不能作为新默认整轮成绩。

BEAM Judge 非法 JSON 字符串反斜杠转义，仅在原生解析明确报告 Invalid escape 时修复，再走同一原生校验器。原始响应和修复计数保留，rubric 与分数不改写。

模型上下文协议不因金额/调度不限而改变：公共 Reader top100、输入 117,760、输出 8,192、安全余量 2,048。其他集成修复见 [README](../README.md)。

发布导出使用 `scripts/export_publication.py --profile benchmark --output <新目录>/benchmark`，只生成本地副本，不提交或推送。
