# 当前 Benchmark 流程

输入版本 proportional-200-v1，seed 17–21，每份 200 题，共 1,000 题、286 个历史任务。固定输入位于 samples/proportional-200-v1/prepared/；原始来源重新抽样见 [数据说明](../datasets/README.md)。

## 执行流程

1. configure_release.py 生成本机路径、模型协议和并发参数。
2. run_parallel.py prepare 导入所选历史、查询与独立评分目标，冻结源码、配置和环境。
3. check 核对冻结身份；start 启动调度。
4. 每份历史各方法独立完成 Add 分块、finalize、Search，持久化记忆和检索证据。
5. Reader 只基于查询与检索证据作答；答案完成后，评分侧才读取对应目标。
6. 报告保存逐题、每 seed、数据集及能力维度得分，并汇总均值与样本标准差。

默认金额与并发不限，用户可设限制。依赖关系仍然保留，先有记忆和检索再回答，再评分。不同方法、历史、运行的状态目录隔离。

## 代码位置

| 功能 | 位置 |
| --- | --- |
| 新抽样算法 | src/memory_benchmark/pipeline/proportional_sampling.py |
| 全母库索引 | src/memory_benchmark/pipeline/source_sampling.py |
| 抽样/固定准备 | scripts/sample_proportional.py、prepare_sampled_seeds.py、prepare_five_seeds.py |
| 调度、共享请求账本、报告 | scripts/run_parallel.py、scripts/parallel_runner/ |
| 原生方法适配 | src/memory_benchmark/methods/ |
| InvMem/ReFind bridge | src/memory_benchmark/runtime/native_bridge/ |
| Reader、Add、评分 | src/memory_benchmark/pipeline/、benchmarks/ |

ReFind 默认观察去重、BEAM JSON 转义修复和本轮兼容性修复详见 [README](../README.md)。部署见 [deployment.md](deployment.md)，运行与恢复见 [运行说明](proportional_parallel_release.md)，统计口径见 [评测契约](evaluation_contract.md)。旧数据和脚本仅在 [旧版本下载](legacy_versions.md) 中提供。
