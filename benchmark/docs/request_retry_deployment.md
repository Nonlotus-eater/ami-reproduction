# 请求重试与预算修复上线说明

日期：2026-10-22。发布位置为 `Nonlotus-eater/ami-reproduction` 的 `benchmark/`。
本次以已发布的 `22c975d` 为基线，保留其请求诊断、队列恢复和单方法并发功能，
补充 transport 重试、预算结算、Add 分块恢复及已有实验的审计接入工具。

## 修复后的行为

原 transport 在一次失败后立即设置 `budget.failed`，同一网关后续请求被拒绝；
单独增大 SDK 重试次数无法解除这一状态。现在由 transport 完成每个逻辑请求的
有限重试，在永久错误或重试耗尽时才锁定失败。OpenAI SDK 始终设置
`max_retries=0`，避免两层重试相乘。

| 项目 | 上线后的规则 |
|---|---|
| 网络超时、网络错误、远端协议错误 | 按上限重试 |
| HTTP 状态 | 408、429、500、502、503、504 可重试，但永久错误码及 `x-should-retry: false` 优先禁止 |
| 供应商通道暂时不可用 | 仅对 503、`model_not_found` 且消息以 `No available channel for model ` 开头的组合按暂时故障处理 |
| 永久错误 | 鉴权失败、配额耗尽、真实模型不存在、参数错误等立即终止；空响应、响应模型不匹配等校验错误也终止 |
| 等待 | 指数退避加随机抖动，单次退避最多 60 秒；合法 `Retry-After` 优先，超过 300 秒时结束当前阶段 |
| 预算与用量 | 每个物理请求单独记录且只结算一次；缺失 usage 保留未知用量及保守预留，不当作零费用 |
| 诊断 | 保留 HTTP 状态、供应商错误码及 SDK 原有异常类型；超时保留原始异常原因 |
| 限速 | 每次实际重试也经过原请求限速并记账，原 worker 总超时仍然生效 |

重试可能增加请求、等待时间和费用；响应丢失时也不能保证供应商侧恰好执行一次。
该修复处理执行可靠性，不改变方法的记忆算法、模型、题目、提示词或评分规则。

## 配置两个开关

在本机 runtime（通常为 `configs/local/runtime.json`）中合并此字段，保留其余配置：

```json
{
  "max_retries": 10
}
```

在该 runtime 的 `env_file` 所指向的本地凭据文件中添加或更新：

```dotenv
AMI_ALLOW_RETRIES=1
```

`max_retries` 接受 0–10 的整数，10 表示首次请求加最多 10 次重试，共最多 11 次。
`AMI_ALLOW_RETRIES` 只接受 `0` 或 `1`；缺省为 `0`。只有开关为 `1` 且次数大于零
才会实际重试。这里读取的是明确指定的凭据文件，仅在终端 `export` 同名变量不足以生效。

公共模板 `configs/runtime.json`、`configs/reproduction/runtime.json` 已设置为 10，
`.env.example` 已加入开关。已有本机配置和凭据不会由 Git 更新覆盖，应在本机核对。
不得将实际凭据文件、`configs/local/` 或 `runs/` 提交到公开仓库。

调度器的 `--passes 10` 是队列重跑上限，与单个请求的重试次数分别计算。
`--parallel 20 --parallel-per-method 5` 控制方法与 seed 队列并发，也不表示每个请求开 20 次重试。

## 新实验部署

先按 [交接文档](model_handoff_99.md) 安装四方法依赖并准备数据。在 fork 根目录进入
`benchmark/`，更新到包含本次修复的提交，然后执行：

```bash
git pull --ff-only
.venv/bin/python -m pip install -e '.[api,data]' 'numpy==2.2.6'
.venv/bin/python scripts/configure_release.py --output configs/local/retry-v1
```

生成器输出目录必须不存在；按配置说明核对其中的数据和方法路径。
确认生成的 `runtime.json` 为 `max_retries=10`，且其 `env_file` 指向的文件已设置开关。
如需 Add 分块恢复，在冻结前额外设置
`"memory_checkpoint_policy": "add_chunks_sqlite_snapshot_v1"`，详见
[分块恢复说明](memory_checkpoint_recovery.md)。该功能不因启用请求重试而自动开启。

```bash
PYTHONPATH=src:. .venv/bin/python scripts/prepare_five_seeds.py \
  --suite configs/local/retry-v1/suite.json \
  --runtime configs/local/retry-v1/runtime.json \
  --output runs/prepared-retry-v1

PYTHONPATH=src:. .venv/bin/python scripts/run_five_seed_experiments.py \
  --prepared runs/prepared-retry-v1 --config configs/local/retry-v1 \
  --output runs/four-methods-retry-v1 \
  --parallel 20 --parallel-per-method 5 --passes 10
```

首次运行会调用真实 API。新实验中，使用公共 Gateway 的记忆、Answer、Judge 阶段均采用
冻结的重试策略。参数或开关变化需要新的运行目录；不要改写旧 prepared 或 manifest。

## 已有实验：下一次记忆 worker 启动时接入

下面的工具针对本框架生成的 Linux 实验目录（含 `results/launch_plan.json`、
各队列 manifest 和冻结源码），必须在运行实验的同一主机上执行。
先在原 runtime 指向的本地凭据文件中启用 `AMI_ALLOW_RETRIES=1`，保持旧 runtime 原文件。

若尚未安装分块恢复 overlay，先验证并接入：

```bash
PYTHONPATH=src:. .venv/bin/python scripts/enable_memory_checkpoints.py \
  --experiment runs/your-experiment
PYTHONPATH=src:. .venv/bin/python scripts/enable_memory_checkpoints.py \
  --experiment runs/your-experiment --apply --handoff-supervisor
```

已有有效分块恢复 overlay 时直接执行下面两条命令。第一条只验证，第二条应用：

```bash
PYTHONPATH=src:. .venv/bin/python scripts/enable_request_retries.py \
  --experiment runs/your-experiment
PYTHONPATH=src:. .venv/bin/python scripts/enable_request_retries.py \
  --experiment runs/your-experiment --apply
```

已安装旧版请求重试 overlay 时，两条命令都追加 `--upgrade`。
工具会验证原始输入和已安装代码哈希，仅短暂停止协调进程，在文件更新后继续调度；
不向活动记忆 worker 发信号。安装过程出错会恢复此前文件并继续协调进程。

当前 worker 继续使用已经导入的版本，新启动的记忆 worker 才启用重试。
旧实验的 Answer/Judge 保留原协议，原始模型与存储身份保持兼容，已有 Add 检查点仍可使用。
其变更范围与新实验不同，报告时应注明切换时间及 `new_memory_workers` 范围。

每队列 `execution_overlay.json` 保存文件哈希和有效策略；
实验的 `request-retry-rollout-*/` 保存之前的文件、收据和答案/评分前缀指纹，
`operations.jsonl` 记录操作。保留这些文件以支持后续验证和续跑。

## 验收与已完成验证

从发布副本的 `benchmark/` 运行公共回归，不调用真实模型：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

部署后检查新记忆 attempt 的 `usage.json`：
`request_retry_policy.owner=transport`、`classification_version=2`、
`effective_max_retries=10`、`max_attempts=11`、`sdk_max_retries=0`。
旧实验接入还应存在 `memory_retry_activation`。故障后同一 `request_id` 出现
递增的 `retry_count` 并最终返回 200，才能证明实际请求重试成功。

截至 2026-10-22 本次发布前已完成：

- 从公开 fork 基线生成的完整 core 发布副本运行 178 项公共回归，全部通过，无跳过；验证进程禁止非本机网络连接。
- SDK 1.59.6、2.3.0、2.54.0 共 208 次针对运行时、重试和恢复的测试执行，全部通过。
- 每个 SDK 均验证 5 个并发网关各先收到一次本地 503、再收到 200：共 10 个 HTTP 请求，峰值并发 5，全部恢复。
- 四方法原生环境的 Add 提交后中断与检索阶段恢复测试全部通过，模型边界采用离线替身。
- InvMem 真实 API 小测通过：实际发送 1 次 LLM、3 次 embedding 请求，均为 200，Add/Search 返回 2 条证据；另有 2 次 503 在本地注入，未发送至供应商。
- 本地运行中的 20 个队列已校验 overlay；一次上线前快照中，15 个新记忆 worker 已采用 v2 策略，28 次实际重试中有 24 个逻辑请求恢复成功，包括真实 429 和临时通道 503。该数字是观察快照，不代表全部任务完成。

这些结果验证重试和恢复逻辑，不代表六数据集正式成绩，也不代表已在全新服务器重装四方法。

## 回退

源码回退：在独立、干净的发布 checkout 中对本次发布提交执行 `git revert <发布提交>`，
完成回归后发布回退提交。实验已冻结的源码不会因此自动改变。

后续新实验关闭重试：把本机 runtime 的 `max_retries` 设为 0，重新冻结到新目录。
如同时关闭 `.env` 中的开关，应使用独立凭据文件和独立新配置，避免影响共享该文件的活动实验。
显式冻结重试策略的实验会校验开关和策略身份，不能把改开关当作透明热回退。

活动 overlay 回退：当前工具支持安装失败时的自动回退，尚未提供已成功安装后的在线卸载命令。
不要直接 checkout 冻结文件、删除收据或删除检查点。需要退回旧执行策略时，在维护窗口先保存
审计目录和当前结果，停止协调进程并等待活动 worker 结束，再用原版本、新输出目录启动实验；
原实验保留供审计。现有结果与新目录结果需分别统计。

## 代码位置

| 内容 | 路径 |
|---|---|
| 错误分类、次数及等待策略 | `src/memory_benchmark/runtime/retry.py` |
| 物理请求重试、预算结算与诊断 | `src/memory_benchmark/runtime/budget.py` |
| SDK 重试关闭、用量输出、策略身份 | `runtime/model_client.py`、`runtime/profile.py`（均在上述源码目录内） |
| 旧实验记忆网关与安装 | `src/memory_benchmark/runtime/memory_retry.py`、`scripts/enable_request_retries.py` |
| Add 检查点及执行变更核验 | `src/memory_benchmark/runtime/memory_checkpoint.py`、`runtime/execution_overlay.py` |
| 分块恢复与 supervisor 接管 | `scripts/enable_memory_checkpoints.py`、`scripts/resume_with_memory_checkpoints.py` |
| 回归 | `tests/test_retry.py`、`tests/test_retry_rollout.py`、`tests/test_memory_checkpoint.py`、`tests/test_checkpoint_rollout.py` |
