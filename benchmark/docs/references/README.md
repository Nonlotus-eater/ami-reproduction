# 抽样历史基准

`AMI_Benchmark_99_reference.docx` 保存旧 99 题报告的表三，曾用于核对历史题数、来源构成和能力标签。对应旧抽样审计脚本已清理；当前 `sample_small_benchmark.py` 从公开来源与代码映射抽样，不读取这个 Word；发布副本不包含参考 Word。

固定五轮部署使用 `configs/reproduction/seed-*.json` 与 `scripts/prepare_five_seeds.py`，不依赖这个 Word，也不依赖当前论文正文。
