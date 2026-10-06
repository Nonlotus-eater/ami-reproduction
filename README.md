# ActiveMemoryIndex reproduction baseline

`reproduction-v1` 是 Xuhao Lin 的 [ActiveMemoryIndex](https://github.com/linxuhao/ActiveMemoryIndex) 的复现，不声明原创算法贡献。目标为 AML Cycle 2 **Textual Memory / Open-source Methods**。复制来源、prior work、文件指纹与改动见 [ATTRIBUTION.md](ATTRIBUTION.md)、[CHANGELOG.md](CHANGELOG.md)、[LICENSE](LICENSE)。

**当前状态：离线复现工程基线已验证；真实 academic 模型链路、官方评测和公网服务尚未验证。** fake 模式仅验证接口与工程行为，不能用于正式评测或声称复现上游分数。现阶段未调用付费模型、未运行 benchmark/AML Smoke/Full、未发布 GitHub、未提交 Evaluation Access Request。

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
