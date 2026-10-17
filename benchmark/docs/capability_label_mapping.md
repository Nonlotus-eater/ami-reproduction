# 原生题型到 AML 小类的本地映射

这些是本项目人工编写、由程序批量执行的规则，不是 AML 官方逐题标签，也不是逐题人工或 LLM 语义标注。

流程：读取原生题型字段 → 查下表得到一个小类 → 按 taxonomy 查所属大类。当前每题只映射一个小类；该标签用于抽样配额和分数分组，不作为 Mem0 的输入。

| 数据集 | 来源字段 | 原生标签或条件 | AML 小类 | 小类说明 | 大类 |
|---|---|---|---|---|---|
| BEAM | `probing_questions 类型键` | `abstention` | H1 | 拒答、未知与证据不足 | H |
| BEAM | `probing_questions 类型键` | `contradiction_resolution` | D2 | 矛盾检测与冲突消解 | D |
| BEAM | `probing_questions 类型键` | `event_ordering` | C2 | 事件顺序/阶段排序 | C |
| BEAM | `probing_questions 类型键` | `information_extraction` | A3 | 结构化字段/集合/枚举抽取 | A |
| BEAM | `probing_questions 类型键` | `instruction_following` | G5 | 指令/格式约束遵循 | G |
| BEAM | `probing_questions 类型键` | `knowledge_update` | D1 | 新值覆盖与当前状态 | D |
| BEAM | `probing_questions 类型键` | `multi_session_reasoning` | B1 | 跨片段/跨 session 多证据聚合 | B |
| BEAM | `probing_questions 类型键` | `preference_following` | E1 | 偏好回忆与个性化遵循 | E |
| BEAM | `probing_questions 类型键` | `summarization` | F1 | 摘要与压缩/长历史全局综合 | D |
| BEAM | `probing_questions 类型键` | `temporal_reasoning` | C1 | 日期、相对时间、间隔 | C |
| CLBench | `metadata.context_category` | `Domain Knowledge Reasoning` | G1 | 领域知识推理 | G |
| CLBench | `metadata.context_category` | `Empirical Discovery & Simulation` | G4 | 经验数据发现与模拟 | G |
| CLBench | `metadata.context_category` | `Procedural Task Execution` | G3 | 程序/工作流执行 | G |
| CLBench | `metadata.context_category` | `Rule System Application` | G2 | 规则系统应用 | G |
| LoCoMo-Refined | `category` | `1` | B1 | 跨片段/跨 session 多证据聚合 | B |
| LoCoMo-Refined | `category` | `2` | C1 | 日期、相对时间、间隔 | C |
| LoCoMo-Refined | `category` | `3` | B4 | 受控开放域/常识补全 | B |
| LoCoMo-Refined | `category` | `4` | A1 | 单点事实/属性与来源回忆 | A |
| LongMemEval-S | `question_id` | `以 _abs 结尾（优先于 question_type）` | H1 | 拒答、未知与证据不足 | H |
| LongMemEval-S | `question_type` | `knowledge-update` | D1 | 新值覆盖与当前状态 | D |
| LongMemEval-S | `question_type` | `multi-session` | B1 | 跨片段/跨 session 多证据聚合 | B |
| LongMemEval-S | `question_type` | `single-session-assistant` | A1 | 单点事实/属性与来源回忆 | A |
| LongMemEval-S | `question_type` | `single-session-preference` | E1 | 偏好回忆与个性化遵循 | E |
| LongMemEval-S | `question_type` | `single-session-user` | A1 | 单点事实/属性与来源回忆 | A |
| LongMemEval-S | `question_type` | `temporal-reasoning` | C1 | 日期、相对时间、间隔 | C |
| PersonaMem v1 | `question_type` | `generalizing_to_new_scenarios` | E1 | 偏好回忆与个性化遵循 | E |
| PersonaMem v1 | `question_type` | `provide_preference_aligned_recommendations` | E1 | 偏好回忆与个性化遵循 | E |
| PersonaMem v1 | `question_type` | `recall_user_shared_facts` | A1 | 单点事实/属性与来源回忆 | A |
| PersonaMem v1 | `question_type` | `recalling_facts_mentioned_by_the_user` | A1 | 单点事实/属性与来源回忆 | A |
| PersonaMem v1 | `question_type` | `recalling_the_reasons_behind_previous_updates` | C3 | 事件/立场轨迹变化 | C |
| PersonaMem v1 | `question_type` | `suggest_new_ideas` | E1 | 偏好回忆与个性化遵循 | E |
| PersonaMem v1 | `question_type` | `track_full_preference_evolution` | C3 | 事件/立场轨迹变化 | C |
| PersonaMem v2 | `pref_type` | `anti_stereotypical_pref` | E2 | 刻板/反刻板偏好鲁棒性 | E |
| PersonaMem v2 | `pref_type` | `ask_to_forget` | D3 | 删除、遗忘与抑制 | D |
| PersonaMem v2 | `pref_type` | `health_and_medical_conditions` | E3 | 个人背景、健康与心理关怀 | E |
| PersonaMem v2 | `pref_type` | `neutral_preferences` | E1 | 偏好回忆与个性化遵循 | E |
| PersonaMem v2 | `pref_type` | `sensitive_info` | H2 | 敏感信息最小披露/隐私 | H |
| PersonaMem v2 | `pref_type` | `stereotypical_pref` | E2 | 刻板/反刻板偏好鲁棒性 | E |
| PersonaMem v2 | `pref_type` | `therapy_background` | E3 | 个人背景、健康与心理关怀 | E |

当前规则覆盖 A/B/C/D/E/G/H 七个大类的 21 个小类；F1 归 D。A2/B2/B3 没有对应规则，不能据此认定原题不涉及这些能力。

“映射核验通过”仅表示原生字段、题目来源和程序规则一致，不等于已验证每道题的能力归属。若要确认语义可靠性，需要结合题干、历史证据及评分要求逐题审核；当前没有这项审核的准确率数据。

依据：[taxonomy](../sources/taxonomy.json)、[来源审计](../sources/source_audit.json)、[LoCoMo 映射](../src/memory_benchmark/benchmarks/locomo_refined/capability_map.json)、[LongMemEval 映射](../src/memory_benchmark/benchmarks/longmemeval/capability_map.json)、[其余映射](../src/memory_benchmark/benchmarks/public_datasets.py)。
