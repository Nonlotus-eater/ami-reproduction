# 当前配置

- `parallel/four-methods.json`：新并发入口模板。金额、任务/题目并发、接口 inflight/RPM/TPM 默认 null（不限）；ReFind 默认去重。
- `proportional-200-v1/`：抽样策略、seed 17–21 固定 ID、来源指纹、数据集/题型/能力分布验收表。
- `runtime.json`：当前 AML Reader、Judge 与 Add 协议。
- `datasets/text_suite.json`：完整六数据集母库路径模板；只在重新抽样或重建时使用。
- `datasets/public_sources.json`：公开数据版本及下载来源。
- `local/`：`configure_release.py` 生成的本机配置，不随发布分发。

旧 reproduction 配置和旧抽样策略从当前发布包移除，见 [旧版本下载](../docs/legacy_versions.md)。
