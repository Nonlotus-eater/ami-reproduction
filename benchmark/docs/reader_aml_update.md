# Reader 更新（2026-10-23）

- 按 [AML 公开规则](https://agentmemories.ai/api-guide)：`top_k=100`，完整输入预算 **117,760**、输出额度 **8,192**、安全余量 **2,048 tokens**。
- 参数在 `configs/runtime.json`；`answer_max_tokens` 由 Reader 作为 `max_tokens` 传给模型调用层。配置生成器会继承这些默认值。
- 超预算时只保留按检索排名排列的证据文本前缀；问题、选项、指令和归档的原始 Search 响应保持完整。
- 升级已有部署时须重新生成或同步本机 runtime，并重新 prepare 到新目录。旧 prepared、运行记录和历史复现配置保留原参数。

字段与操作细节见[配置说明](../configs/README.md#运行配置)。
