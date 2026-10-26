# 固定比例抽样五 seed

这里是可移植的模型输入包，不含实验答案或模型评分结果。`prepared/seed-17` 至 `seed-21` 每份含 200 题，共 1,000 道不同题目、286 个历史任务。原始历史和查询/评分目标分文件保存；评分目标只给评测侧使用。

使用 `scripts/run_parallel.py prepare` 导入时，会核验输入哈希并绑定当前安装的代码及运行参数。包内 manifest 的 framework 字段刻意不冒充当前代码身份，不能跳过导入直接当作已冻结的运行目录。

`BUNDLE.json` 是文件哈希清单。题目 ID、母库来源指纹、数据集/原生题型/能力比例表位于 `configs/proportional-200-v1/`。原始内容来自六个公开数据版本，来源和归属沿用仓库 `sources/`、`datasets/README.md`。

默认六数据集题数：81 / 47 / 178 / 55 / 470 / 169。BEAM 仅选 100K，不覆盖 500K/1M。详细命令与限制见 [运行说明](../../docs/proportional_parallel_release.md)。
