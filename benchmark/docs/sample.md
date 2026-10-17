# Seed 17：历史、QA pairs 与抽样过程

`samples/seed-17/` 保存了系列测试中 seed 17 使用的整轮输入：22 份历史、202 组 QA pairs，覆盖六个数据版本。目录里的历史沿用原有消息、角色和顺序；每道问题保留原来的题目 ID、题干、选项和评分目标。

可以先打开 [index.json](../samples/seed-17/index.json) 查看全部历史与题目的对应关系。本文按数据集各展示一个案例。

## 这一轮是怎样抽出来的

五轮抽样使用 seed 17–21。文本母库共有 10,649 道题，将 BEAM 限定为 100K 历史后，有 9,249 道候选题。抽样先决定读哪些历史，再从这些历史关联的问题中选题。

seed 17 的过程如下：

1. 按 seed 决定的随机顺序，从 LoCoMo 选 2 份历史、LongMemEval 选 7 份、CLBench 选 4 份、PersonaMem v1/v2 各选 2 份、BEAM 选 2 份，得到最初的 19 份历史。
2. 检查这些历史关联的题目能否覆盖各个有题的小类。本轮又补充了 4 份 CLBench 历史，共有 23 份历史进入候选范围，关联 317 道题。
3. 先为每个有题的小类保留至少 2 题，再从同一大类中选取其他题目，每个大类最多 40 题。选题范围来自前面选中的历史，题目按 seed 决定的随机顺序取出。
4. 最终得到 202 道题，分布在 22 份历史上。最初选中的一份 LongMemEval 历史没有题目进入最终清单。

抽样时还设置了每轮历史最多 30 份、总题数至少 91 的参数。历史按各数据集原有的单位保留：LongMemEval 每题保留独立 haystack，PersonaMem v1 按题目指定的历史前缀取出消息，BEAM 保留完整 100K 对话。

现在运行这五轮时，准备程序直接读取已经选好的题目 ID。seed 17 的清单在 [configs/reproduction/seed-17.json](../configs/reproduction/seed-17.json)，抽样参数见 [sampling.json](../configs/reproduction/sampling.json)。

| 数据版本 | 实际使用的历史 | QA pairs | 本轮数据的组织方式 |
|---|---:|---:|---|
| LoCoMo-Refined | 2 | 94 | 一份 conversation 对应多道题 |
| LongMemEval-S | 6 | 6 | 每道题对应自己的完整 haystack |
| CLBench | 8 | 8 | 一份 task 上下文对应一个最终任务 |
| PersonaMem v1 | 2 | 2 | 按 persona、共享历史和前缀位置组织 |
| PersonaMem v2 | 2 | 55 | 一份完整 persona 历史对应多道生成式问题 |
| BEAM 100K | 2 | 37 | 一份完整对话对应多个 probing questions |
| 合计 | **22** | **202** | |

## 文件怎样组织

```text
samples/
├── README.md
└── seed-17/
    ├── index.json
    ├── locomo_refined/<case>/
    ├── longmemeval_s/<case>/
    ├── clbench/<case>/
    ├── personamem_v1/<case>/
    ├── personamem_v2/<case>/
    └── beam/<case>/
        ├── history.json
        └── qa_pairs.jsonl
```

每个 `<case>/` 下都有一份 `history.json` 和一份 `qa_pairs.jsonl`。目录名沿用 prepared 输入中的 case 标识；`index.json` 同时列出可读的 `episode_id`、会话数、消息数、题目数量和 QA ID，便于查找。

### History

`history.json` 保存一份历史，内部由 session 和 message 组成。

| 字段 | 含义 |
|---|---|
| `benchmark` | 数据集标识，例如 `locomo_refined` |
| `episode_id` | 这份历史的原生来源标识 |
| `speakers` | 对话双方的名称或角色名称 |
| `sessions` | 按输入顺序排列的会话或消息块 |
| `sessions[].session_id` | 当前会话的标识 |
| `sessions[].timestamp` | 规范化时间；来源没有日期时为 `null` |
| `sessions[].raw_timestamp` | 来源提供的原始时间文本 |
| `sessions[].source_session_id` | 原生会话标识，供 LongMemEval 等数据使用 |
| `sessions[].messages` | 当前会话中的完整消息列表 |
| `messages[].source_id` | 消息标识，可与题目中的证据 ID 对照 |
| `messages[].role` / `speaker` | 消息角色与发言人 |
| `messages[].text` / `timestamp` | 消息正文及时间 |

LoCoMo 的 session 对应带日期的对话场次；BEAM 的 session 保留原始消息批次；PersonaMem 和 CLBench 则按消息顺序组织为输入块。

### QA pair

`qa_pairs.jsonl` 每行是一组配对记录，包含 `query` 和 `target` 两个对象。这里的 A 对应作者数据中的参考答案或评分要求。

| 对象 | 常用字段 | 含义 |
|---|---|---|
| `query` | `benchmark`、`episode_id`、`qa_id` | 题目所属数据集、历史及题目 ID |
| `query` | `text` | 题干或最终用户任务 |
| `query` | `timestamp` | 题目发生时间 |
| `query` | `system_prompt` | 该题保留的系统提示词 |
| `query` | `all_options` | PersonaMem v1 等选择题的候选回复 |
| `query` | `evaluation_mode` | 例如 PersonaMem v2 的 `generative` |
| `target` | `qa_id` | 与 `query.qa_id` 对应 |
| `target` | `gold` | 参考答案、正确选项或用户偏好 |
| `target` | `evidence_source_ids` | 来源提供的证据消息或会话 ID |
| `target` | `question_type` | 数据集原生题型 |
| `target` | `capability_leaves` | 题型映射得到的能力小类 |
| `target` | `scoring_data` | rubric、偏好及其他评分字段 |

阅读样例时，Query 和 Target 放在同一条记录里；实际 Pipeline 整理输入时，会分别写入 `queries.jsonl` 和 `targets.eval.jsonl`。记忆和作答阶段使用历史与 Query，评分阶段读取 Target。方法生成的回答另存于运行目录的 `answers.jsonl`。

## 六个具体案例

### LoCoMo：从对话时间推算年份

历史 `conv-43` 中有 29 个 session、680 条消息，本轮为它选了 46 道题。其中 `conv-43#q0007` 问：

> What year did John start surfing?

在 [history.json](../samples/seed-17/locomo_refined/bccf0df17178793e32717c9a/history.json) 中可以找到这条消息：

```json
{
  "source_id": "D3:27",
  "role": "assistant",
  "speaker": "John",
  "text": "I started surfing five years ago and it's been great. I love the connection to nature.",
  "timestamp": "2023-07-16T16:21:00+00:00"
}
```

这句话说于 2023 年，结合 “five years ago” 得到 2018 年。对应的 QA pair 为：

```json
{
  "query": {
    "benchmark": "locomo_refined",
    "episode_id": "conv-43",
    "qa_id": "conv-43#q0007",
    "text": "What year did John start surfing?",
    "timestamp": null,
    "system_prompt": "",
    "all_options": "",
    "evaluation_mode": ""
  },
  "target": {
    "qa_id": "conv-43#q0007",
    "gold": ["2018"],
    "capability_leaves": ["C1"],
    "evidence_source_ids": ["D3:27"],
    "question_type": "2",
    "scoring_data": {}
  }
}
```

这组数据把题目、参考答案和原始证据连在了一起。该历史的全部题目见 [qa_pairs.jsonl](../samples/seed-17/locomo_refined/bccf0df17178793e32717c9a/qa_pairs.jsonl)。

### LongMemEval：从多个会话合并事实

题目 `gpt4_ab202e7f` 问：

> How many kitchen items did I replace or fix?

它对应一份包含 46 个 session、481 条消息的 [完整历史](../samples/seed-17/longmemeval_s/1d647ba92debfd880a6f903b/history.json)。厨房物品的变化散落在不同的做饭聊天中：

| 原生会话 ID | 与问题有关的事实 |
|---|---|
| `answer_728deb4d_1` | 更换厨房水龙头 |
| `answer_728deb4d_2` | 更换厨房地垫 |
| `answer_728deb4d_3` | 将旧 toaster 换成 toaster oven |
| `answer_728deb4d_4` | 将旧 coffee maker 换成 espresso machine |
| `answer_728deb4d_5` | 修好厨房置物架 |

[QA pair](../samples/seed-17/longmemeval_s/1d647ba92debfd880a6f903b/qa_pairs.jsonl) 中的参考答案为：

> I replaced or fixed five items: the kitchen faucet, the kitchen mat, the toaster, the coffee maker, and the kitchen shelves.

这道题需要从多次会话中收集信息，并将同一物品的重复提及合并后计数。`target.evidence_source_ids` 列出上述五个会话 ID。

### CLBench：按任务要求检查回复

任务 `5ed0a3e7-cbed-453f-a68d-25e67ac3ebc3` 涉及一份 `training_efficiency` 数据。用户已经观察到训练时长与生产力得分的相关性为 0.65，希望用 Linear Regression 做预测。

它的 [history.json](../samples/seed-17/clbench/d4c731ea61274d22fda44633/history.json) 保存研究员角色和回复要求；[qa_pairs.jsonl](../samples/seed-17/clbench/d4c731ea61274d22fda44633/qa_pairs.jsonl) 的 `query.text` 包含用户的分析思路和完整表格数据。

这道题有 13 条 rubric，其中两条是：

> The response should explicitly state a candidate model specification, justifying each of the included predictors.

> The response should describe exactly how categorical variables will be encoded.

这些要求保存在 `target.scoring_data.rubrics` 中。评分时逐条检查回复，再按 CLBench 的整组要求计算该题得分。

### PersonaMem v1：在历史前缀后选择合适回复

历史 ID 为 `17:1ee4be06b43a991eeb7c78772fdf2a1b795f46f336b5fc46a5c711d07cc1c052:prefix:103`。其中 `prefix:103` 表示使用共享上下文的前 103 条消息，文件中按顺序组织为 54 个消息块。

这道题的题型是 `track_full_preference_evolution`。用户谈到重新投入文学活动的经历，候选回复回顾其阅读偏好变化。`query.all_options` 保存全部候选回复，`target.gold` 为 `(b)`。

阅读这类题时，可以把 [历史](../samples/seed-17/personamem_v1/02e07685d76d5aa491c93474/history.json) 与 [QA pair](../samples/seed-17/personamem_v1/02e07685d76d5aa491c93474/qa_pairs.jsonl) 放在一起：前者给出截至该题的经历，后者给出当前消息、候选回复和正确选项。

### PersonaMem v2：将历史偏好带入新问题

题目 `personamem_v2:1677` 问：

> What are some fun and active ways to spend a weekend afternoon in my neighborhood?

它来自 persona 866 的历史。这份 [history.json](../samples/seed-17/personamem_v2/b501e77576c4008e5c26f665/history.json) 有 290 条消息；用户曾在一封待润色的邮件里写道：

> I'll be free after my usual morning swim at the MIT pool.

[QA pair](../samples/seed-17/personamem_v2/b501e77576c4008e5c26f665/qa_pairs.jsonl) 把 `Enjoys swimming at local pools` 作为参考偏好，`evaluation_mode` 为 `generative`。作答模型据此生成适合用户的周末建议，评分程序评估回复对这项偏好的遵循。这份历史在 seed 17 中共对应 23 道题。

### BEAM：用 rubric 表达答案要点

`100K:12` 是一份包含 392 条消息的 [完整对话](../samples/seed-17/beam/106cfb3dbb5ac3d6df56bb00/history.json)，本轮有 18 道题使用它。其中 `beam:100K:12:knowledge_update:0` 问：

> By what date am I aiming to complete all my onboarding modules?

对应的评分字段为：

```json
{
  "question_type": "knowledge_update",
  "scoring_data": {
    "rubric_nuggets": [
      "LLM response should state: April 22"
    ],
    "length_split": "100K"
  }
}
```

BEAM 将答案要点放在 `rubric_nuggets` 中。评分器对每条要点评分后取平均，这道题的要点是 “April 22”。同一历史还包含信息抽取、时间推理、偏好等题型，完整配对见 [qa_pairs.jsonl](../samples/seed-17/beam/106cfb3dbb5ac3d6df56bb00/qa_pairs.jsonl)。

## 把样例接回 Pipeline

阅读一份样例的顺序可以是：先在 `index.json` 找到历史，再打开 `history.json` 了解对话，接着查看 `qa_pairs.jsonl` 中关联的问题和评分目标。这样就能看到 Pipeline 在记忆写入、问题检索和评分三个阶段各自使用的内容。

整体运行过程见 [Benchmark 功能与运行流程](benchmark_guide.md)，安装和实际执行命令见 [model_handoff_99.md](model_handoff_99.md)。
