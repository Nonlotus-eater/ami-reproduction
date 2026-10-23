# 评测契约

本次发布的 AMI、Mem0、InvMem、ReFind 共用规范输入、证据格式、Answer、评分与汇总，保留各自原生记忆和存储机制。
默认恢复报告 v1 策略（仓库历史名称 v3）：seed 17–21，历史最多 30、可用小类至少 2、大类最多 40。`configs/reproduction/` 的固定清单与此前 GitHub 发布一致；重新抽样的同规则配额见 [small_benchmark_v1.json](../configs/small_benchmark_v1.json)，部署见 [当前指导](model_handoff_99.md)。

## 数据与主指标

| 数据版本 | 主指标 | 单题规则 |
|---|---|---|
| LoCoMo-Refined | judged accuracy | CORRECT=1，WRONG=0 |
| LongMemEval-S | judged accuracy | CORRECT=1，WRONG=0 |
| CLBench | strict accuracy | 全部原生 rubric 要求满足为 1，否则 0 |
| BEAM | rubric mean | 每条 rubric 为 0/0.5/1，对题内 rubric 取均值 |
| PersonaMem v1 | MCQ accuracy | 原生选项解析与匹配，0/1 |
| PersonaMem v2 | preference adherence | 固定生成式模式，原生偏好遵循评分 [0,1] |

BEAM 的 event-ordering 另保存排序诊断，不替代 rubric 主分。
PersonaMem v2 不把同题 MCQ 和生成式重复计入 Overall。

评分模板与解析来自 AML 固定 commit `1b8142bfe0f20f1c5218d6b554aa0012de34e504`，
源码与 SHA-256 审计保存在 [sources/](../sources/)，执行时检查来源 hash。

## 隔离和生命周期

记忆端只收到历史和查询；gold、rubric、正确选项、证据标签和能力标签只留在评测端。
候选选项属于查询输入，正确选项标签不进入记忆。
所有问题在历史构建和 finalize 后执行，协议为 offline-history-QA；不声称复现 Streaming。
MemMA 自检问题来自可见历史，不使用测试题或 gold。
不同运行、方法、历史实例使用独立 scope。

历史依作者单位归属：LoCoMo/BEAM 是 conversation，LongMemEval 是独立题目 haystack，
PersonaMem v1 是原生合法前缀，v2 是完整 persona 文本历史，CLBench 是 context/task。
来源选择和 QA 清单在运行前冻结，每轮四方法必须相同；历史 SHA-256 与题库一致。
未知日期保持未知，原始无时区日期按声明的 UTC 解释，不能用评测当天替代。
LongMemEval 保留重复会话的 source_session_id，并对出现次数作唯一化；时间异常记录于数据审计。

## 模型和上下文

聊天模型固定 GPT-4o mini；响应仅接受 allowlist 中的模型标识。
使用 embedding 的 AMI/Mem0/InvMem 共用 env 指定的 text-embedding-v4，当前 1024 维、每批最多 10 段；ReFind 保留原生 BM25/agent。
长文本使用共同 UTF-8 分块、归一化、byte 权重池化；不回退本地 embedding。
模型、endpoint、维度和预处理进入 provider identity。

检索证据保留方法原生返回顺序，tokenizer 为 o200k_base。当前公共默认的 top_k、Reader 证据预算、完整输入上限为 null，不额外截断。
四方法已验证的实验配置另存于 `configs/reproduction/runtime.json`，分别为 100、4096、119000；有预算时跳过放不下的整条证据。
InvMem/ReFind 固定原生 HTTP schema 要求整数 top_k；当前 adapter 对 null 按本任务作用域的实际记忆数量调用同一原生检索实现。配置生成器默认保留 top_k=100，但 Reader 证据预算与完整输入上限均为 null，并启用模型窗口分段读取；旧实验参数仅在显式选择复现 runtime 时应用，不声称历史成绩来自当前无截断默认条件。
调用量和费用写入运行账本，没有次数或费用 cap；API 自动重试为 0。
输入预算、模型输出长度和超时是独立约束。

## 新运行的可靠性策略（2026-10-20）

公共默认及新抽样配置启用 `model_window_v2`：完整提示词按模型 tokenizer 计数，保留封装安全余量与至少 1,024 token 的回答空间（显式输出上限更小时遵守该上限）。证据超出可用窗口时分段读取；中间摘要截断时仅拆分失败片段并复用成功摘要。所有来源文字进入分段处理，不声称摘要在语义上绝不遗漏。

CLBench 默认使用编号 yes/no 的严格结构协议，校验每项编号及总分一致性；协议无效或截断时最多重评 3 次，首次有效结果即停止，包括有效的 0 分。评分标准不变，不修补模型输出或按分数选择重评结果。

普通文本 Add 采用 `aml_20_messages_2000_words_v1`，每块不超过 20 条消息或 2,000 个来源文本的 Unicode 空白分隔词。长单条消息按词边界无损拆分；不跨来源 session 合并，保留 role、时间戳和原始 `session_id`，以独立确定性 chunk ID 区分写入。每个 worker 保存 `add_plan.json`，包含来源字符范围及分段哈希。

阈值来自 [AML API Guide](https://agentmemories.ai/api-guide) 与[官方中文文档](https://agentmemories.ai/zh-cn/docs)。公开页面说明词数由冻结 Adapter 计数，但未发布其完整实现；本地计词口径明确版本化，不声称逐字节复刻官方内部边界。适配器为原生方法补充的说话人标签及提示词不进入来源词数。

AMI 在启动前检查所选源码对实际输入角色的支持，默认选择已有 system 兼容目录。历史运行的冻结源码、配置、输入与成绩不受这些新默认策略影响；新增 Add 分段和 Reader/Judge 策略应作为新实验条件记录。

## 分数与能力

每个 benchmark 分数为冻结题集逐题主分的均值乘 100；本地 Overall 是六个 benchmark 分数的等权均值。
未完成任一冻结题目的有效评分时，完整 Overall 为 null，另报已评分均值和覆盖率。
网络、模型身份、解析或 worker 故障产生 unscored，不计为方法答错。

能力使用 [taxonomy.json](../sources/taxonomy.json) 的 A/B/C/D/E/G/H 大类和 24 个细类，F1 归 D。
题目映射是本地粗映射，尚无官方逐题标签或官方聚合规则的复现保证。
A2/B2/B3 没有验证过的公开代理题；D3/H2 目前仅反映回答中的遵循行为。
没有题目的能力为 N/A，不能补零。小来源样本与重复测量不能视作额外独立样本。

## 原生方法范围

Mem0 使用 OSS Memory、graph off；AMI 使用 reproduction release profile 和 system role 兼容补丁。
InvMem 保留原生 hybrid Dense+BM25/RRF、窗口检索；ReFind 保留原生四动作 agent、BM25/session RRF，不改成向量 baseline。
这些是固定本地配置，不直接声称为论文默认设置。
源码与补丁见 [发布版本清单](../sources/publication-baselines.lock.json)；保留各原生仓库作者归属与许可。

各 worker 记录实际 Python、依赖版本、输入 hash 与方法配置。
本次不运行 LightMem 系列，不需要 LLMLingua 压缩权重或本地 GPU；远程 API 调用不由本地 GPU 提速。
