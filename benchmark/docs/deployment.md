# 新五 seed 部署

需要 Linux、Python 3.11 和 bubblewrap（例如 apt install bubblewrap）。以下命令在 benchmark/ 根目录执行。固定输入已随包提供，无需旧版目录或全量来源。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[api,data,http_memory]' 'numpy==2.2.6'
.venv/bin/python -m pip check
cp .env.example .env
chmod 600 .env
```

填写自己的聊天和 embedding endpoint/key。原生源码提交与补丁以 [baseline lock](../sources/baselines.lock.json) 为准。

## 原生方法安装

只用 lock 固定提交；原生源码不包含在发布包中，分别获取并创建环境。已有目录先检查版本及本地差异，避免覆盖。

```bash
mkdir -p baselines
git clone https://github.com/mem0ai/mem0.git baselines/mem0
git -C baselines/mem0 checkout 71fba8d46436f88569d600f81a55208c38ad30b5
git clone https://github.com/Nonlotus-eater/ami-reproduction.git baselines/ami-reproduction
git -C baselines/ami-reproduction checkout e545fdb10ce139bb19dc7c6f814ef853ed19cba0
git -C baselines/ami-reproduction apply ../../patches/ami-system-role.patch
git clone https://github.com/wenxiaof345-ctrl/vanilla-rag-memory.git baselines/InvMem
git -C baselines/InvMem checkout 31ab7bf9cfa3ee3c4f986e82f6e7a00b134ba8ca
git -C baselines/InvMem apply ../../patches/invmem-native-interface.patch
git clone https://github.com/imlrz/ReFind.git baselines/ReFind
git -C baselines/ReFind checkout a80175ca0eeb52a938d7cab7a602bc780de8a577
git -C baselines/ReFind apply ../../patches/refind-native-interface.patch

python3.11 -m venv baselines/mem0/.venv
baselines/mem0/.venv/bin/python -m pip install -e baselines/mem0 -e '.[api]' \
  'fastembed==0.7.4' 'openai==2.3.0' 'httpx==0.28.1' 'numpy==2.2.6'
python3.11 -m venv baselines/ami-reproduction/.venv
baselines/ami-reproduction/.venv/bin/python -m pip install \
  -r baselines/ami-reproduction/requirements-academic.lock.txt -e '.[api]'
python3.11 -m venv baselines/InvMem/.benchmark-env
baselines/InvMem/.benchmark-env/bin/python -m pip install -e '.[api]' \
  'numpy==2.2.6' 'faiss-cpu==1.15.1' 'fastapi==0.141.1' 'pydantic==2.13.4' 'httpx==0.28.1'
python3.11 -m venv baselines/ReFind/.benchmark-env
baselines/ReFind/.benchmark-env/bin/python -m pip install -e '.[api]' \
  'fastapi==0.141.1' 'pydantic==2.13.4' 'httpx==0.28.1'
```

可用 `--ami-root ..` 指向 fork 根 AMI，前提是确认其固定版本/兼容补丁；默认独立 clone 可保留根目录原状。
InvMem adapter 把其 `src/` 加入方法进程路径，注入共享 API 向量，未启用本地神经模型，不需安装上游 Torch/SentenceTransformer 全量可选依赖。ReFind 保留 agent/planner。
Mem0 的 FastEmbed 用于原生 BM25，不能与 LLMLingua 混淆；首次配置应准备 `models/fastembed` 缓存：

```bash
baselines/mem0/.venv/bin/python - <<'PY'
from pathlib import Path
from fastembed import SparseTextEmbedding
cache = Path('models/fastembed').resolve()
cache.mkdir(parents=True, exist_ok=True)
encoder = SparseTextEmbedding(model_name='Qdrant/bm25', cache_dir=str(cache))
assert len(next(encoder.embed(['Alice lives in Kyoto.'])).indices) > 0
print('BM25 cache verified')
PY
```

上述准备可能下载依赖/缓存，不调用付费模型。保留各原生仓库作者归属与许可。InvMem 固定提交未发现根 LICENSE，不为它额外宣称许可证；本次分发小型适配代码和补丁，原生实现单独获取。

## 生成配置并启动

```bash
.venv/bin/python scripts/configure_release.py --output configs/local
.venv/bin/python scripts/run_parallel.py prepare --config configs/local/parallel.json
.venv/bin/python scripts/run_parallel.py check --config configs/local/parallel.json
.venv/bin/python scripts/run_parallel.py start --config configs/local/parallel.json
```

默认金额、任务并发和接口限速均为 null，不设上限；可在 prepare 前编辑 parallel.json，或配置生成时使用 --history-workers、--per-method-workers、--query-workers、--budget-cny。
原生方法自己的内部线程/批处理参数仍遵循方法配置，可按需要调整，不替换方法算法。
配置冻结后需要改参数时，请使用新输出目录和账本目录。停止、恢复、报告和 API 重试见 [运行说明](proportional_parallel_release.md)。旧版本见 [下载位置](legacy_versions.md)。
