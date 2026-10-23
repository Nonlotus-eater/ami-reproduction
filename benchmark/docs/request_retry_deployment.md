# 请求重试与 Add 断点恢复上线说明

发布位置：`Nonlotus-eater/ami-reproduction/benchmark/`。基于 `22c975d`，保留已有错误诊断和恢复功能。

## 请求重试与预算修复

此前，transport 一次失败就会锁定 `budget.failed`，后续重试被立即拒绝。现在由 transport 统一完成有限重试，永久错误或重试耗尽后才锁定失败；SDK 的 `max_retries` 保持为 0，避免两层重试叠加。

超时、限流和服务暂时故障可以重试；鉴权、参数错误及真实模型不存在等永久错误立即终止。每次实际请求独立记录用量、只结算一次；缺失 usage 时保留未知用量及保守预留。

## 两个配置项

| 位置 | 配置 | 含义 |
|---|---|---|
| `configs/local/runtime.json` | `"max_retries": 10` | 首次请求失败后最多再试 10 次，共最多 11 次请求 |
| runtime 的 `env_file` 指向的 `.env` | `AMI_ALLOW_RETRIES=1` | 启用请求重试；设为 0 则关闭 |

两项共同生效。公共 runtime 模板和 `.env.example` 已补充这些配置，已有本机文件需自行核对；修改它们不会自动更新旧实验的冻结配置。

## Add 断点恢复

此前，Add 中断后重跑会从头重建整份历史的记忆库。启用分块恢复后，每个 Add 块完成后保存检查点，重跑时恢复最近的完整快照，跳过已保存分块。

例如，100 个 Add 块中，前 39 块已保存、第 40 块失败，重跑从第 40 块继续，前 39 块无需重复 Add 或承担这些 Add 的模型调用费用。

恢复以完整分块为单位：失败块或尚未保存检查点的块需要重做。全部 Add 和 finalize 已保存后，检索失败只重跑检索，不再建库。

该功能支持 AMI、Mem0、InvMem、ReFind，需通过 `memory_checkpoint_policy: "add_chunks_sqlite_snapshot_v1"` 或已有实验的分块恢复接入工具启用。没有检查点时仍从头开始。详见 [Add 分块恢复说明](memory_checkpoint_recovery.md)。
