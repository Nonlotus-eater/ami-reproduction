# 同份历史内的 Add 分块恢复

在准备新输入前，将 runtime 中的 `memory_checkpoint_policy` 设置为
`add_chunks_sqlite_snapshot_v1`。这是可选配置；
已有 prepared 输入仍使用各自冻结的 runtime。支持 AMI、Mem0、InvMem、ReFind
四个同步、本地数据库适配器。

每次成功 Add 后，worker 对适配器的全部持久状态保存一致快照，再原子更新
已完成的连续分块数。SQLite 使用 backup API，包含已经提交的 WAL 内容；
快照期间不会启动新的 Add、finalize 或 search。Mem0 的 Qdrant 本地集合、
实体集合、集合元数据和历史数据库一起保存。

重试仍产生新的 `memory_attempt_NNN`，保留独立日志和调用用量，但从同一历史的
`memory_resume/checkpoint.json` 所指向的快照恢复到新 attempt 的 `memory_state/`。
用户 scope、请求 ID、原生记录 ID 以及已提交的状态保持连续。成功完成的分块
不再调用模型。失败块即使已经部分写库，也会被丢弃，恢复完整快照后重做。
进程在“数据库提交之后、快照发布之前”退出，也采用这一规则。

所有 Add 和 finalize 完成后另存检查点。之后的检索失败只会重新检索，
不重复建库。当前版本不会逐题恢复部分检索结果；Answer/Judge 原有恢复保持不变。
快照增加本地磁盘读写，不增加模型请求。

恢复时校验历史、问题、分块计划、运行配置、服务身份、适配器代码及原生代码
身份；发现不一致、快照文件被修改或同份历史已有 worker 持锁时，拒绝复用。
未实现持久化重开的适配器与内存型离线 fixture 不支持该策略。

## 已运行实验的接入

`scripts/enable_memory_checkpoints.py --experiment <目录>` 只验证安装条件。
加 `--apply --handoff-supervisor` 后，会归档原始文件和答案/评分前缀指纹，在队列的
`execution_overlay.json` 中记录明确的原始与新文件哈希，并启用后续新启动的
记忆 worker。原始 prepared、方法配置、模型、评分与启动计划不修改。

安装时仅短暂停止协调进程以避免启动时读到一半更新；不向运行中的记忆 worker
发送信号。文件检查失败时回滚安装并恢复协调进程。原冻结代码保留，后续队列
重启会逐项核对这次记录的执行变更，其余源码继续按原始指纹验证。

为兼容本轮旧调度器对桥接文件的校验，安装后接管 supervisor，保留活动队列
PID、重试计数和并发限制。`resume.sh` 改由
`scripts/resume_with_memory_checkpoints.py` 调用原版调度器；新启动的队列明确使用
自身已记录变更的冻结框架。原启动计划、发布源码和队列 manifest 保持原文件。

正在运行的旧 worker 继续原流程。它的部分数据库不会被事后当作完整检查点；
同一历史下一次启动新 worker 时才开始形成可恢复的 Add 前缀。

测试包括 SQLite/WAL、多库存取一致性、快照发布故障、提交后强制退出、恢复
身份与文件完整性、并发锁，以及有活动 worker 的安装/回滚。四种原生环境还
分别执行：第二块原生 Add 写入后强制退出 → 从第一块恢复 → 检索后强制退出
→ 不调用 Add 完成检索。模型边界使用离线 fixture，不调用付费 API。
