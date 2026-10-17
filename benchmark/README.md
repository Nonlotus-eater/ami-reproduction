# Agent Memory Benchmark

六个公开文本数据版本的共同评测框架：LoCoMo-Refined、LongMemEval-S、CLBench、PersonaMem v1/v2、BEAM。
本次发布支持 **ActiveMemoryIndex（AMI）、Mem0、InvMem、ReFind**。各方法原生构建记忆、检索，共用 Answer、原生评分器和汇总。

当前固定题集为五轮 v3：seed 17–21 分别 **202 / 199 / 196 / 201 / 205 题**，合计 1,003 题次、791 道不同题目、每方法 112 个历史任务。每轮独立构建记忆，BEAM 使用完整 100K 历史。旧 99 题是历史版本。

部署从 [当前模型交接指导](docs/model_handoff_99.md) 开始。该文件保留旧文件名，内容已更新为四方法、五轮指导。
上传范围、目标 fork 布局和导出命令见 [发布范围](docs/publication_scope.md)。

## 离线验证

从 Benchmark 根目录执行，Python 3.11：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[api,data]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m memory_benchmark demo --output runs/demo-new
```

demo 使用人工数据，不调用模型，不能代表方法成绩。

## 配置与数据

复制 `.env.example` 到本地 `.env` 后填写凭据。真实 `.env`、原始数据、模型缓存和运行结果不属于发布内容。

```bash
.venv/bin/python scripts/configure_release.py --output configs/local
```

生成四方法配置、数据路径和已验证的实验运行配置。按 [数据说明](datasets/README.md) 准备完整来源文件、调整 `configs/local/suite.json`，再离线冻结题集：

```bash
.venv/bin/python scripts/prepare_five_seeds.py --output runs/prepared-five-seeds-new
```

该工具校验固定的 210 个来源文件及逐轮题目 ID，恢复五轮完整历史和评分目标，不依赖本地旧 `runs/`、99 题迁移包或论文 Word 正文。

`configs/runtime.json` 是当前无额外检索/输入截断的默认配置；`configs/reproduction/runtime.json` 是四方法已验证的有界实验配置。**两者不是同一个实验条件**，详见 [配置说明](configs/README.md)。InvMem/ReFind 的固定原生接口不接受 `top_k=null`。

## 结构

```text
src/memory_benchmark/    数据接口、公共 Answer/Judge、评分、队列
integrations/           InvMem/ReFind 适配器与独立 worker/队列桥接
configs/reproduction/   五轮参数、逐轮 ID、来源指纹、复现配置
sources/                固定评分源码、来源审计、taxonomy、baseline 版本
patches/                AMI / InvMem / ReFind 原生兼容补丁
scripts/                配置、下载、离线 prepare、发布导出
tests/、examples/       公共层回归与人工离线数据
```

原生仓库和依赖环境单独安装；ReFind 保留原生 BM25/agent 检索，不使用 embedding API。
评分规则、信息隔离、能力标签局限与六数据集宏平均见 [评测契约](docs/evaluation_contract.md)。

依赖和凭据配置后，可按用户授权运行 `.venv/bin/python scripts/release_smoke.py --output runs/real-api-config-new`，验证四方法真实 API 最小链路。该命令会产生模型费用，每方法三道人工题，不代表 Benchmark 成绩。

本次全新 clone、五套全新环境及四方法真实 API 验证已通过，见 [配置验证记录](docs/configuration_validation_20261017.md)。
