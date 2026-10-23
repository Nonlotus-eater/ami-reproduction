# 请求重试与 Add 断点恢复上线说明

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
