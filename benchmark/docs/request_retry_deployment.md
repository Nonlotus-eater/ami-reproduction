# 请求重试与 Add 断点恢复上线说明

功能上线 commit：[1703af8](https://github.com/Nonlotus-eater/ami-reproduction/commit/1703af8)。

发布位置：`Nonlotus-eater/ami-reproduction/benchmark/`。基于 `22c975d`，保留已有错误诊断和恢复功能。

## 请求重试与预算修复

**transport** 是本项目放在 OpenAI SDK 与模型 API 之间的 HTTP 传输层，负责实际发送请求、重试和用量记账。此前它一次失败就会锁定 `budget.failed`，后续重试被立即拒绝；现在由它执行有限重试，仅在请求最终失败时锁定。

**SDK 的 `max_retries`** 控制 OpenAI Python SDK 遇到可重试错误后自动再次发送请求的次数，设为 0 表示关闭 SDK 自身的重试。本项目固定为 0，由 transport 统一重试，避免两层叠加；runtime 中同名的 `max_retries` 控制的是 transport 的次数上限。

超时、限流和服务暂时故障可以重试；鉴权、参数错误及真实模型不存在等永久错误立即终止。每次实际请求独立记录用量、只结算一次；缺失 usage 时保留未知用量及保守预留。

## 两个配置项

| 位置 | 配置 | 含义 |
|---|---|---|
| `configs/local/runtime.json` | `"max_retries": 10` | 首次请求失败后最多再试 10 次，共最多 11 次请求 |
| runtime 的 `env_file` 指向的 `.env` | `AMI_ALLOW_RETRIES=1` | 启用请求重试；设为 0 则关闭 |

`AMI_ALLOW_RETRIES` 是本项目的开关，写在 `.env` 中，名称须全大写；runtime.json 通过 `env_file` 指定读取哪个文件。它不由 OpenAI SDK 直接读取。

程序会读取 `.env` 中的重试开关和 `runtime.json` 中的重试次数。开关为 `1`、次数为 `10` 时，单个请求遇到暂时性错误，最多再试 10 次；开关为 `0` 或未填写时，不重试。

公共 runtime 模板和 `.env.example` 已补充配置，已有本机文件需自行核对。修改文件不会自动更新已创建客户端或旧实验的冻结配置。

## Add 断点恢复

此前，Add 中断后重跑会从头重建整份历史的记忆库。启用分块恢复后，每个 Add 块完成后保存检查点，重跑时恢复最近的完整快照，跳过已保存分块。

例如，100 个 Add 块中，前 39 块已保存、第 40 块失败，重跑从第 40 块继续，前 39 块无需重复 Add 或承担这些 Add 的模型调用费用。

恢复以完整分块为单位：失败块或尚未保存检查点的块需要重做。全部 Add 和 finalize 已保存后，检索失败只重跑检索，不再建库。

该功能支持 AMI、Mem0、InvMem、ReFind，需通过 `memory_checkpoint_policy: "add_chunks_sqlite_snapshot_v1"` 或已有实验的分块恢复接入工具启用。没有检查点时仍从头开始。详见 [Add 分块恢复说明](memory_checkpoint_recovery.md)。

## 2026-10-24：Reader 与审查第 1、3、6 项更新说明

本次发布补齐记忆程序的文件隔离、原生配置冻结和记忆 HTTP 接口重试，并确认 Reader 预算、裁剪和模型直连逻辑一并包含在 GitHub `main` 的 `benchmark/` 中。

| 更新 | 修复后的行为 | 举例 |
| --- | --- | --- |
| 第 1 项：答案文件隔离 | runner、queue 和原生 bridge 的记忆 worker 统一通过 bubblewrap 启动，只挂载当前任务必需文件。缺少隔离工具时直接失败；协调器拒绝符号链接等异常输出文件 | 记忆程序知道答案文件的绝对路径，也无法打开它；其他 seed 的答案和原始数据集同样不挂载 |
| 第 3 项：配置冻结 | 除 Python 源码外，配置模板、JSON/YAML 等运行资源、非秘密参数和 Python 环境版本也参与校验。worker 使用只读的 `frozen_native` 副本 | 抽取预算从 1200 改成 4000，会被识别为参数变化，旧队列不会悄悄按新预算继续 |
| 第 6 项：记忆接口重试 | AML HTTP 客户端分别处理 Add/Search 的可重试错误，最多尝试 32 次，保持同一次调用的 ID 和请求体不变。429 遵循最多 60 秒的 Retry-After，缺失或无效时等待 60 秒；永久错误和坏格式响应立即失败 | Add 遇到暂时性 409 可以重试，Search 遇到 409 则立即失败。HTTP 转发后端默认只转发一次，避免嵌套放大重试 |
| Reader 默认预算 | `top_k=100`，证据预算不再额外限制为 4096；完整输入 117760、输出 8192、安全余量 2048 tokens。配置生成器继承这些默认值 | 超预算时按检索排名保留证据的 token 前缀，问题、选项、指令和归档 Search 响应保持完整 |
| Reader/评分网络 | 模型 HTTP 客户端设置 `trust_env=False`，直接连接配置的模型接口；Answer、Judge、embedding 均忽略系统代理 | 关闭 IDE/终端中配置的本机代理后，服务器仍可直接请求模型接口 |

记忆 Add/Search 的 32 次请求上限，与模型 transport 的 `max_retries` 是两个不同层面的设置。本次没有将模型重试叠加为 32 倍。

完整修复集合使用 `benchmark` 发布范围：四个原生方法及 AML HTTP 客户端。安装 Python 依赖可用 `pip install -e '.[api,data,http_memory]'`；文件隔离还要求 Linux、bubblewrap 和可用的用户/PID 命名空间。`core` 是不含 HTTP 客户端的精简范围，不能用于验证第 6 项。

新实验应重新生成本机 runtime 并 prepare 到新目录。历史 prepared 和已运行队列保留原冻结代码、参数和结果；更新源码不会自动改变旧实验。需要让已有实验改为直连时，使用配置说明中的 `scripts.disable_model_proxy` 工具；它适用于含 `experiment.json`、`launch.sh` 和 `results/launch_plan.json` 的完整实验目录，且要求记忆检索已经完成。

验证覆盖：worker 尝试读取合成答案、符号链接和 `/proc` 绕过；配置预算变化与冻结副本篡改；Add/Search 状态码、请求内容和重试次数；故障代理下 Answer/Judge/embedding 直连；Reader 的实际请求预算。测试使用合成数据、MockTransport 和模型替身，不作为方法得分或正式 AML 评测成绩。

本次发布包验证结果：公共回归 201 项全部通过；四个原生方法各 3 项集成检查，共 12 项全部通过，包含文件隔离中的实际 Add/Search 与断点恢复。另检查了导出副本可再次导出、发布文件 SHA-256 和凭据扫描。
