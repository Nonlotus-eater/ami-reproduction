# ActiveMemoryIndex reproduction baseline

`reproduction-v1.1`（原始基线标签 reproduction-v1）是 Xuhao Lin 的 [ActiveMemoryIndex](https://github.com/linxuhao/ActiveMemoryIndex) 的复现，不声明原创算法贡献。目标为 AML Cycle 2 **Textual Memory / Open-source Methods**。复制来源、prior work、文件指纹与改动见 [ATTRIBUTION.md](ATTRIBUTION.md)、[CHANGELOG.md](CHANGELOG.md)、[LICENSE](LICENSE)。

**当前状态：离线工程基线与一次最小真实 synthetic Add/Search 链路均已验证。** 本轮 Add/Search 均 HTTP 200，raw+fact 持久化与即时检索通过；本轮 LLM 3 次、Embedding 3 次，历史累计 LLM 5 次、Embedding 4 次。详情和成本见 REPRODUCTION_STATUS.md。未运行 benchmark/AML Smoke/Full、未发布 GitHub、未提交 Evaluation Access Request。fake 模式与本次小样本均不能证明复现上游分数；下文保留的失败/待验证记录为阶段历史，以末尾成功记录为准。

队友接手与创建 GitHub 仓库，请先看 [TEAM_HANDOFF.md](TEAM_HANDOFF.md)。

## 目录与来源

- `app/`：上游实现；仅 `main.py` 增加 Textual 输入校验，其余代码逐字一致。
- `tests/`、`scripts/`：上游测试与工具，保留原样。`scripts/smoke_contract.py` 是本地 HTTP 检查，**不是官方 AML Smoke**；指向真实服务时会产生模型费用。
- `upstream/`：原 README、RELEASE、SUBMISSION、Docker 文件以及快照清单、精确代码补丁。其“我们／原创／测试成绩”均指上游作者。
- `reproduction/`：本项目环境启动、fake SDK 和本地验证工具；不是新的记忆算法。
- `.env.academic.example`：上游模板原样存档；`.env.example`：用户权限运行模板，凭据留空。
- `requirements.txt`：上游完整依赖；`requirements-academic.txt`：远程模型所需的轻量依赖；`requirements-academic.lock.txt`：本机验证时的完整依赖版本。
- `REPRODUCTION_STATUS.md`：环境、验证、限制与待确认事项。

未复制 benchmark 数据、gold、研究结果或研究文档到运行仓库。上游完整快照中的文件哈希仍收录于 manifest，用于定位来源，不表示这些文件都被 vendored。

## 环境安装（无 sudo）

已在 Linux、Python **3.10.20** 测试。宿主默认 Python 3.13 不适用于上游 NumPy 1.26.4 的常规 wheel 安装。当前使用现有 Conda 环境提供的解释器创建独立 venv，不修改该 Conda 环境：

```bash
cd /data1/sp/Agent-Memory/ami-reproduction
/home/sp/miniconda3/envs/siglip_mm/bin/python -m venv .venv
.venv/bin/python -m pip install -r requirements-academic.lock.txt
.venv/bin/python -m pip check
```

在其他机器可使用 Python 3.10，或自行创建用户级 Conda Python 3.10 环境后创建 venv。依赖下载需要能访问配置的 PyPI 镜像；内网机器可由获授权的联网机器准备适合目标 Python/平台的 wheelhouse，再执行 `pip install --no-index --find-links <wheelhouse> -r requirements-academic.lock.txt`。本项目未下载模型权重。

academic 路径不使用本地 BGE 或 cross-encoder，因此轻量环境省略 `sentence-transformers`、`huggingface-hub` 和 torch；保留完整 upstream requirements 供历史研究，但未验证那些可选运行路径。上游 Dockerfile 会下载 BGE 和 torch，不适合作为本阶段的安装入口。Docker 客户端存在，当前用户不能访问 daemon，未构建镜像。

## 不付费的本地验证

```bash
cd /data1/sp/Agent-Memory/ami-reproduction
.venv/bin/python -m reproduction.run_tests
.venv/bin/python -m reproduction.check_http
```

第一条逐个运行 26 个 upstream standalone test 文件（混合 unittest 与脚本断言），避免误用 pytest discovery。清除继承的模型配置/凭据，给子进程安装禁止出站 socket 的 guard。

第二条启动真实 Uvicorn HTTP 服务，绑定 `127.0.0.1` 的可用端口，运行 contract 检查与 upstream 本地检查脚本，重启并验证持久化，然后自动停止服务、清理临时数据库。鉴权 token 随机生成，只在测试进程内存／子进程环境中传递，不输出或存盘。详细日志写入被 git 忽略的 `artifacts/`。

已通过：37 个新增 HTTP 检查、38 个 upstream 本地 HTTP 检查；26 个 upstream 文件全部退出 0（输出合计 330 个 PASS 标签／unittest cases，**不是 330 个官方合同测试**）。覆盖健康检查、ID echo、即时检索、两侧均有数据的用户隔离、跨会话检索、幂等、重启、数量限制、响应结构、鉴权、必填字段和无效输入。

手动持续运行 fake 服务：

```bash
cd /data1/sp/Agent-Memory/ami-reproduction
export AMI_REPRO_TEST_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python -m reproduction.serve --mode fake --port 8012
```

在有同一环境变量的客户端 shell 中，用 `Authorization: Bearer <AMI_REPRO_TEST_TOKEN 的值>` 调用 Add/Search；不要把真实服务密钥放进该变量。若端口占用，用 `--port 0` 自动选择空闲端口，以 Uvicorn 日志为准。Ctrl-C 停止。匿名 `/health` 可直接访问。

fake 模式完全覆盖继承的 AMI/OpenAI/DashScope 配置，只读取公开模板，使用不可用的 `offline.invalid` endpoint 和显式 fixture 字符串；SDK 替换发生在启动前，额外禁止出站 socket。向量是确定性词哈希，事实是假响应中复制的用户语句，recall 是固定模板；更新 stage 1 返回空列表。**这些都不模拟真实模型能力**。它运行上游抽取解析、embedding 分段/校验/归一化、检索、SQLite 和响应代码；显式更新的正反例由 upstream fake 单元测试覆盖。fake 默认数据库为 `data/fake.sqlite3`，embedding identity 使用不同 endpoint，不能混用于真实 v4 数据库。

若执行环境禁止 localhost/socketpair，需给这些本地测试进程相应权限；这与能否访问公网无关。

## Academic profile：待授权的真实启动

`reproduction-v1` 保留上游 rc3 academic 参数：

| 项目 | 配置 |
|---|---|
| LLM | `gpt-4o-mini`；并发 8 |
| Embedding | `text-embedding-v4`，1024 维，batch 10，并发 4，timeout 20 秒，retry 1 |
| 表示 | raw turns + extracted facts；抽取开启 |
| Query / fusion | recall 开启，weight 0.5 |
| 返回 | limit 100，400000 字符预算，raw-first，邻居半径 1 |
| 更新 | detect/render/withhold 开启，version 7 |
| Admission | Add 16，Search 0，wait 45 秒，Retry-After 10 |
| 其他研究开关 | 与上游模板一致，关闭 |

本地 real 数据库默认 `./data/memory-v4.sqlite3`；端口通过启动命令 `--port` 传入（默认 8012），启动器始终绑定 loopback、单 worker。数据库文件及 WAL/SHM 必须可写，长期运行应保留整个 data 目录；不应多 worker 共享当前缓存实现。

需要单独配置的字段：

- `OPENAI_API_KEY`：LLM 凭据；`AMI_LLM_MODEL=gpt-4o-mini`。
- 如需内网兼容网关，显式配置 `OPENAI_BASE_URL`，必须仍提供规定模型；未做连通性验证。
- `AMI_EMBED_API_KEY`、`AMI_EMBED_BASE_URL`：embedding 独立凭据和对应区域/工作区 endpoint。示例为 DashScope Singapore，不推断它适用于你的账户。
- `AMI_AUTH_TOKEN`：服务鉴权 token，与模型 key 分开；模板为空。
- 数据库路径与可用端口。

**获得付费调用授权后**，安全地在本机准备被 git 忽略的配置文件，再运行：

```bash
# 以下仅是未来操作说明；本阶段没有执行。
cp .env.example .env
chmod 600 .env
# 由用户在本机安全填写配置，不在聊天/仓库中粘贴密钥。
.venv/bin/python -m reproduction.serve --mode academic --env-file .env --port 8012 --allow-paid-api
```

没有 `--allow-paid-api` 时启动器在导入 app、读取凭据或创建数据库之前拒绝启动。有该参数也仍需有效配置；此参数是操作防误触，不代替用户授权。启动本身会调用 embedding warm-up，Add/Search 会继续计费。

## API 与已知边界

`GET /health` 匿名返回 liveness；`POST /add` 与 `POST /search` 使用鉴权。完整字段见保存的 upstream README，当前赛事理解以本项目 `../COMPETITION_UNDERSTANDING.md` 为依据，AMI 实现不等于官方规则。

Add 必填 `request_id/messages/user_id/session_id`，回显三个 ID；支持 `user`／`assistant` 的非空文本及可选毫秒时间戳。Search 必填 `query/user_id/top_k`，可选 `options`，响应 `{"data":[{"id":"...","content":"...","score":0.0}]}`；`created_at` 可选。正式 top_k 为 100，结果数不超过请求值。

仍保留 upstream 边界：request_id 幂等以同 user、同请求体重试为前提，不校验变更后的请求体；single-worker cache 的同用户并发读写、提交后异常恢复、Full 容量与真实 provider 故障仍需专项验证。健康检查不是模型质量证明。更新失败处理的细节以代码为准，并非所有失败都使整个 Search 取消 withholding。

## 发布与 Evaluation Access Request

内部草案见 [EVALUATION_ACCESS_DRAFT.md](EVALUATION_ACCESS_DRAFT.md)。当前具备可审阅代码、来源披露、配置模板与本地接口证据；尚缺真实模型最小验证、公开仓库/固定 commit、对外 HTTPS endpoint、运行容量信息和平台要求的最终字段。不声明已获官方认证或完成正式申请。

以下均须另行确认：付费 API、小规模真实链路验证、较大 benchmark、公开 GitHub push、公网部署、AML Smoke、AML Full、正式提交申请。Full 配额有限，不用于日常调试。

## 本机中转站配置补充（post-v1）

用户提供的实际地址已写入私密 `.env`：LLM 为 `https://linkapi.ai/v1`；embedding 为 `https://maas.qianwenaiapi.com/api/v1/services/embeddings/text-embedding/text-embedding`。后者是完整 DashScope-native endpoint，不是 OpenAI-compatible base URL，不可直接给 OpenAI SDK 拼接 `/embeddings`。

本地配置用 `AMI_REPRO_EMBED_PROTOCOL=dashscope-native` 显式启用 reproduction HTTP bridge；`AMI_EMBED_BASE_URL` 在该模式下保存完整 endpoint。保持 `AMI_EMBED_BACKEND=openai` 以复用 upstream 的远程向量处理；bridge 替换网络客户端及声明 transport identity，**不修改 embedding 算法或模型**。原模板默认 OpenAI-compatible 分支继续保留。真实 endpoint 的协议兼容性需由授权联调确认，未在本步探测。

`import dashscope` 是导入阿里云的 Python SDK，不是加载本地模型；SDK 也需要凭据和正确 endpoint。仅导入 SDK 不会自动采用另一个 curl 示例里的中转地址。本项目直接用已有 HTTPX 发送原生 JSON，不需安装该 SDK。[SDK 官方源码](https://github.com/dashscope/dashscope-sdk-python)、[API 协议文档](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)。

新增 adapter 的六个纯内存测试可单独运行：`.venv/bin/python -m unittest reproduction.test_native_embedding -v`。它们不消耗真实 API 配额。用户已授权的最小真实联调仍需用专用计数/限额/首错终止入口；请勿直接用普通 serve 入口替代该入口。

### 最新覆盖：百炼北京官方兼容接口

用户随后确认直接使用阿里云百炼北京 API Key。当前私密配置已切回 `AMI_REPRO_EMBED_PROTOCOL=openai`，上面的原生 bridge 不启用。

`AMI_EMBED_BASE_URL=https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` 必须替换为本账户的实际工作空间 URL；启动器会拒绝含未替换占位符的 URL。保持 `AMI_EMBED_MODEL=text-embedding-v4` 和 1024 维，不采用示例里的 `qwen3.7-text-embedding`。密钥可放在 `AMI_EMBED_API_KEY` 或 `DASHSCOPE_API_KEY`，二者都配置时前者优先。其余 academic 算法配置与无重试限制不变。该兼容接口及模型维度依据[阿里云官方文档](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)，实际连通性仍未调用验证。

### 本次真实最小联调已停止

已授权的 one-shot 执行实际使用 Embedding 1 次、LLM 1 次。Embedding 预热成功；LLM 返回 HTTP 200，但 response model 不匹配或缺失，触发严格停止。Add 返回本地 503，未持久化，Search 未执行。详情见 REPRODUCTION_STATUS.md。`reproduction/real_minimal.py` 的 attempt.lock 防止自动重跑；当前不要清除 lock 或重发请求。下一次真实尝试需要用户明确授权。

### 已定位并修复：模型别名与固定版本的误判

后续经用户授权的单次诊断发现：请求 `gpt-4o-mini`，返回 `gpt-4o-mini-2024-07-18`，HTTP 200。[OpenAI 官方模型文档](https://developers.openai.com/api/docs/models/gpt-4o-mini)确认后者为 GPT-4o mini 的快照。现已修复 reproduction guard，只接受该明确别名/快照映射，仍拒绝其他模型、未知后缀和缺失 model。8 项离线保护测试通过。

本次只增加一个 LLM 诊断请求，没有重跑 Add/Search；累计真实 LLM 2 次、embedding 1 次。完整真实链路尚未完成；已失败的第一次报告和 one-shot lock 保留，不能将本次诊断成功等同于 Add/Search 成功。

### 最新：最小真实 Add/Search 已成功

在用户再次明确授权后，唯一一次完成尝试已通过：1 Add + 1 Search 均 HTTP 200，SQLite raw=1、fact=1，立即检索返回 2 条 synthetic memories。本轮真实 LLM 3 次、embedding 3 次，含历史诊断总计 5/4 次；无重试。模型快照映射已通过真实响应验证。按用户价格估算本轮 ¥0.0002309（未折扣）。详细证据与限制见 REPRODUCTION_STATUS.md 末节。

`real-minimal.py --authorized-completion` 对应的 one-shot lock 已创建，不能再次直接运行。最小验证已经完成，当前不再发真实 API 请求；后续扩展、benchmark、官方评测与外部发布仍需各自授权。
