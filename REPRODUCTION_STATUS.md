# Reproduction status

更新：2026-10-06，Phase 3 / reproduction-v1。

**最新结论：本地 reproduction baseline 与已授权的最小真实 Add/Search 联调均已通过。** 本轮 1 次 synthetic Add 和 1 次 Search 均 HTTP 200；新增 LLM 3 次、Embedding 3 次，连同先前诊断累计为 LLM 5 次、Embedding 4 次，零重试、未超限。按用户单价，本轮未折扣费用估算 ¥0.0002309。所有真实调用已停止，未执行 benchmark、官方 Smoke/Full、公开发布或正式申请。下文保留各阶段历史记录，以末尾“完整链路成功”节为最新状态；这不代表 benchmark 质量复现或官方认证。

## 环境检查

| 项目 | 结果 |
|---|---|
| 工作目录 | `/data1/sp/Agent-Memory/ami-reproduction` |
| upstream | `/data1/sp/Agent-Memory/ActiveMemoryIndex-main`；105 文件 SHA-256 清单，检查无变化 |
| 默认 Python / Conda | `/home/sp/miniconda3/bin/python`，3.13.11；Conda 可用 |
| 已有 Conda 环境 | base 3.13.11；cs224n 3.13.15；siglip_mm 3.10.20 |
| 选用运行环境 | `.venv/`，由 siglip_mm Python 3.10.20 创建的独立 venv；未改已有 Conda 环境 |
| sudo | 未使用 |
| Docker | 客户端 28.5.1；当前用户无法访问 `/var/run/docker.sock`，无构建/容器运行 |
| 依赖 | 安装固定 academic 核心版本；完整 freeze 见 requirements-academic.lock.txt；pip check 通过 |
| 核心版本 | FastAPI 0.115.6、Uvicorn 0.34.0、Pydantic 2.10.4、NumPy 1.26.4、OpenAI SDK 1.59.6、HTTPX 0.28.1 |
| 原完整 requirements | 原样保留；轻量运行未安装 SentenceTransformers/HuggingFace/torch，本地 BGE/CE 不在验证范围 |
| 依赖网络 | 沙箱内首次下载被拒；经允许后从配置的清华 PyPI 镜像下载成功 |
| 公网模型连通性 | 未检查；内网环境的代理、区域/网关可达性尚未知 |
| HTTP 测试监听 | 127.0.0.1:58113；重启验证为 127.0.0.1:41881；均已停止 |
| 当前默认启动端口 | 8012；可 `--port 0` 选择空闲端口；只绑定 loopback |
| 数据库 | fake `data/fake.sqlite3`；real `data/memory-v4.sqlite3`；集成测试使用临时目录并清理 |
| 凭据 | 未查找/读取/使用真实 secret，模板全部留空；测试 auth token 在内存随机生成，不存盘；fixture 字符串不是真实密钥 |

## 已完成步骤

1. 阅读赛事理解、AMI 技术分析、upstream README/academic profile/requirements 与相关运行测试代码。
2. 创建独立复现目录，保留 MIT LICENSE、作者、原 repository、prior work 和快照指纹。
3. 复制 app/tests/scripts；保存 upstream 原说明文件；不复制 benchmark/data/gold 到运行仓库。
4. 保持 academic 算法配置：raw+fact、抽取、recall、0.5 fusion、raw-first、radius 1、update v7 均保留。
5. 增加 Textual 角色与非空内容校验，拒绝原实现会静默忽略的空白消息。无算法变更。
6. 创建用户权限独立环境、固定依赖、显式 fake/academic 启动入口。
7. 执行离线 upstream 测试、真实 loopback HTTP fake 检查、重启持久化检查。
8. 完成 README、ATTRIBUTION、CHANGELOG、配置模板、忽略规则和申请信息草案。

## 与 upstream 的差异／修改文件

- **唯一改动的上游代码：**`app/main.py`，增加 `Literal[user,assistant]` 和 nonblank content validator；有效字符串不被 validator 改写。完整 diff：`upstream/LOCAL_CHANGES.patch`。
- **完全相同：**app 其余模块、26 个 tests、3 个 scripts、LICENSE、requirements.txt、.env.academic.example。
- **新增运行/依赖：**`reproduction/__init__.py`、`serve.py`、`run_tests.py`、`check_http.py`；requirements-academic.txt / lock；.env.example（DB 路径与启动注释适配）；.gitignore。
- **新增文档/来源：**README、ATTRIBUTION、CHANGELOG、本状态文件、EVALUATION_ACCESS_DRAFT；upstream 原文档/容器文件备份、manifest、patch。
- **本地忽略产物：**.venv、artifacts 日志/报告、Python bytecode；没有提交真实 env、数据或凭据。
- 未修改两个上一阶段分析文档、AMI 原目录、AML 官方仓库；无参数调优、无新记忆架构。

## 已通过验证

| 验证 | 结果 / 证据 |
|---|---|
| Upstream standalone tests | 26/26 文件 exit 0；输出合计 330 PASS 标签或 unittest cases；`artifacts/unit-tests/summary.json` |
| Upstream 测试范围 | embedding transport/identity、memory/retrieval/order、update v1–v7 guards、cache、user/request idempotency、admission、429/503/failure、optional branches 的 fake 单测 |
| 自建 HTTP 检查 | 37/37；`artifacts/http/summary.json` |
| 原有本地 contract script | 38/38；`artifacts/http/upstream-local-contract.txt`；不是平台 Smoke |
| 持久化 | Add 200 后直接 SQLite 检查 raw+fact；进程重启可检索，重复 Add 不增加行 |
| 隔离 | 空用户、两侧已写入用户、相同 request_id 不同用户；同用户跨 session 检索 |
| contract | /health、/add、/search，三个 ID echo、data[]、可选 created_at、唯一 ID、top_k 0/1/2/100、缺字段 422、无效消息 422 |
| authentication | Add/Search 缺 token / 错 token 401；Bearer/Token/X-API-Key 正确 token 可用；匿名 health |
| dependency check | `pip check` 成功 |
| 源码一致性 | manifest 校验上游未改；app 差异仅记录的 schema 校验 |

测试运行器禁止出站 socket，模型 SDK 使用 fake。初次受限沙箱运行无法创建 socket，async 测试超时，未据此修改算法；得到本地执行权限后完整通过。新 HTTP 测试初次把可选 `created_at` 误设为必填，已修正测试断言，未改响应逻辑。

Fake 的语义模型是确定性词哈希/固定 JSON，不测真实语义质量；HTTP fixture 的更新检测返回空，更新正反例在 upstream 单测中验证。真实 provider warm-up、配额、并发吞吐、retry 传输和长时压力未验证。

## 当前启动与重跑命令

```bash
cd /data1/sp/Agent-Memory/ami-reproduction
.venv/bin/python -m reproduction.run_tests
.venv/bin/python -m reproduction.check_http

# 持续 fake 服务：独立 test token，只在当前 shell 内存中。
export AMI_REPRO_TEST_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python -m reproduction.serve --mode fake --port 8012
```

端口占用用 `--port 0`；Ctrl-C 停止。读取服务须在持有上述临时 token 的客户端使用鉴权。环境若禁止创建 socket，需要给予本地进程权限。

未来真实启动（**未执行，必须先获授权并安全配置**）：

```bash
.venv/bin/python -m reproduction.serve --mode academic --env-file .env --port 8012 --allow-paid-api
```

不加显式 paid 开关会直接拒绝启动。真实启动会产生 embedding warm-up 调用；fake 和 real 数据库及 embedding identity 分离。

## 尚缺配置与下一步

- 用户确认允许的真实 API 调用次数/预算；在本机安全配置独立 LLM key、embedding key、服务 auth token，不发到聊天或仓库。
- 确认内网到 LLM endpoint 和对应区域 DashScope/兼容 embedding endpoint 的可达路径。尚未测试真实公网 API。
- 授权后仅做最小真实 Add/Search 验证，确认模型、1024 维、原文+事实、recall、同步持久化；更新真实调用验证另计调用预算。
- 再准备公网 HTTPS、固定地址、资源/容量说明、运行监控和真实数据的备份/保密安排；当前单 worker 缓存与异常恢复边界不作已解决声明。
- 发布前复核自己的固定版本、公开 repository URL、来源披露。上游 commit 缺失仍需如实披露；file manifest 固定本次实际来源。
- 按当前平台官网表单补齐申请，草案 `EVALUATION_ACCESS_DRAFT.md` 未提交。

## 需要用户确认的事项

当前停在无付费调用边界。下一步首先需要确认：**是否授权最小真实 LLM/Embedding 联调，以及调用次数或预算上限。** 密钥只需在本机安全配置。

大量 benchmark、GitHub public push、正式公网部署、AML Smoke、AML Full、Evaluation Access Request 正式提交均未执行，且各自需要单独明确授权；本次下载免费依赖与本地测试授权不扩展为这些操作的授权。

## 本地版本冻结

本仓库初始化为独立 Git repository；本地 `reproduction-v1` 标签固定本阶段提交。
提交身份为 `Reproduction automation <reproduction@localhost>`（自动生成的工程快照，非原作者署名）。
可用 `git rev-parse reproduction-v1` 取得完整 commit。没有配置公开 origin、没有 push。
这是我们复现仓库的 commit，不是 upstream commit；upstream 仍以 manifest 标识。


## 后续授权更新：最小真实联调准备

用户已授权仅以 synthetic 数据执行 1 次 Add + 1 次 Search；LLM 固定 gpt-4o-mini，Embedding 固定 text-embedding-v4，各自最多 5 次实际 API 请求（embedding startup warm-up 也计入）。不允许自动扩大规模、benchmark/sweep/批量或官方 Smoke/Full。遇重试、401/403、429、5xx、模型不匹配或网络异常立即停止。

用户另已独立验证学校内网服务器经 Cloudflare Quick Tunnel 可由公网访问，故公网不可达不再列为阻塞项；本次不重做 tunnel/公网部署。先前待授权状态属于 Phase 3 初始快照。

当前已创建 git 忽略的私密 .env 模板（权限 600，凭据留空），仅该私密配置把 LLM/Embedding retries 设为 0。真实凭据尚未配置，实际 LLM/Embedding 请求数均为 0；Add/Search 真实联调均未执行。

待用户在本机填写凭据与对应 endpoint 后，需以带全局请求计数、硬上限和首错中止的联调入口执行；不能直接运行原有普通启动器来替代这些约束。用户本次授权已有效，无需再次申请相同范围的付费许可。

## 用户提供的模型接入信息已配置

已在私密 `.env` 填好所有已确定的非凭据配置：LLM `https://linkapi.ai/v1` / `gpt-4o-mini`；Embedding 原生完整 endpoint `https://maas.qianwenaiapi.com/api/v1/services/embeddings/text-embedding/text-embedding` / `text-embedding-v4` / 1024 维。凭据字段未改动、未输出；文件权限仍为 600，仍被 Git 忽略。API 主机地址来自用户示例，未探测其真实能力或声明它们为阿里云/OpenAI 官方域名。

AMI 原适配器使用 OpenAI-compatible JSON；用户给出的 embedding curl 使用 DashScope-native JSON。因此增加 reproduction 层的可选原生 HTTP bridge（AMI_REPRO_EMBED_PROTOCOL=dashscope-native），不改 app/ 或记忆算法。向量的批处理、分段、归一化及 index/维度校验继续由 upstream 执行。native transport 写入独立 embedding identity；不能复用其他 transport 的库。

六个纯 MockTransport 测试已通过，自动重试/重定向关闭、错误信息不回显 provider 响应体或认证头。未安装 dashscope 包；已有 HTTPX 足够发送同格式原生请求。公开文档只用于核对协议，不证明中转站实际能力。

截至本步，真实 LLM API 0 次、Embedding API 0 次；真实 Add/Search 未执行。下一步仍是安全配置凭据后，使用专用全局计数/5+5硬上限/首错终止入口完成已授权的唯一一次 Add/Search。普通 serve 入口没有跨 LLM/Embedding 的全局首错停止与请求预算，故本阶段没有运行它。

## 最新配置：阿里云百炼北京 OpenAI 兼容接口

此更新取代上一节的 embedding 中转站/原生协议配置：用户确认使用阿里云百炼 API Key 与北京地域。私密 `.env` 已切回 AMI 原有 OpenAI 兼容路径（AMI_REPRO_EMBED_PROTOCOL=openai），原生 bridge 保留但不启用。

Embedding model 保持 text-embedding-v4，维度 1024；未使用示例中的 qwen3.7-text-embedding。当前 base URL 为 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，尚需用户提供真实 WorkspaceId 或控制台完整 URL。启动器已增加占位符拒绝检查，避免误发请求；仅对自造配置执行无网络检查并通过。

阿里云密钥可填写 `.env` 中的 AMI_EMBED_API_KEY，或使用 DASHSCOPE_API_KEY（前者非空时优先）。LLM 仍为原配置的 gpt-4o-mini / linkapi.ai。凭据不改动、不输出，retries 均为 0。

真实 LLM/Embedding 请求仍各 0 次，未执行真实 Add/Search。除了凭据，当前所缺非密钥信息是实际 WorkspaceId；不以旧地域地址或假定的默认空间替代。


## Workspace URL 已补齐

用户已提供北京工作空间完整 OpenAI-compatible URL，已替换私密 .env 中的占位符；本地 URL 格式检查通过。模型仍为 text-embedding-v4、1024 维、零重试，native bridge 关闭，凭据未改动或输出。此操作未验证账户授权或真实连通性，实际 LLM/Embedding API 调用仍各 0 次。下一步仍须通过有全局计数/硬上限/首错中止的专用入口执行已授权的单次 synthetic Add/Search；不直接运行普通 serve 入口。

## 唯一一次真实联调结果：遇模型标识校验失败后停止

用户确认价格单位为人民币/百万 tokens 后，在原有授权内执行了一次 synthetic Add 尝试；没有独立模型探测、benchmark、官方 Smoke/Full 或扩大规模。入口为 `reproduction/real_minimal.py`，调用 AMI 实际 startup 和 ASGI Add/Search 路由（进程内 TestClient；未新增公网部署）。此前 loopback HTTP 已单独验证。

| 项目 | 实际结果 |
|---|---|
| Embedding API 请求 | **1 次**，启动 warm-up；HTTP 200，model 精确匹配 text-embedding-v4，1024 维校验通过 |
| Embedding 用量 | 响应 usage：3 input/total tokens；按用户提供 ¥0.5/1M 计算为 ¥0.0000015，账单以供应商为准 |
| LLM API 请求 | **1 次**；HTTP 200，但响应 model 未通过 gpt-4o-mini 精确匹配（不匹配或缺失） |
| LLM 用量/费用 | 本次 guard 在模型校验失败处停止，未记录 usage，不能精确核算；不代表免费 |
| POST /add | 尝试 **1 次**，HTTP 503，未成功；停止后 SQLite items 为 0 |
| POST /search | **0 次**，按首错中止约束未执行 |
| 重试 | **0 次**；另一个并行 LLM 工作在发出请求前被全局失败状态拦截 |
| 超额/外部操作 | 未发生；没有新的真实请求、benchmark、sweep、公开发布或官方评测 |

LLM 的模型标识检查在网络层触发全局停止；SDK 把传输异常封装成连接错误，AMI 将其映射为本地 Add 503。**这不是 provider 返回了 503**：provider 实际 HTTP 200。因此当前不能认定 gpt-4o-mini 模型可用，也不能把本地 503 归因于服务商宕机。

脱敏报告：`artifacts/real-minimal-once/report.json`（被 Git 忽略）。仅存状态码、计数、布尔结果和数字 usage，不保存 headers、API keys 或完整模型响应。报告未保留实际 model 字段值，所以不能进一步区分字段缺失、同系列版本别名或不同模型；此证据缺口如实保留，不补造返回名，也不自动接受别名。

新增调用保护：LLM/Embedding 各 5 次硬上限（含启动预热）、在网络发送前计数、跨两通道串行化 provider 请求、首错全局 latch、禁重试/重定向、严格请求/响应模型与结构检查、写库前失败检查、one-shot lock 拒绝自动重跑。6 项纯 MockTransport 安全测试通过，不消耗真实配额。未改变 app/ 算法或 prompts。

下一步停止在这里。可由用户提供中转站关于响应 model 字段/模型映射的说明来做只读排查；任何再次真实调用需要用户明确授权，不能复用这次已停止的 one-shot 执行去自动补跑。

## 模型标识排查结果与本地修复

用户随后授权排查。仅新增 1 次 synthetic LLM 诊断请求（最多 8 输出 tokens，实际 2），没有 embedding、Add 或 Search 调用；零重试、零跳转。实际 HTTP 200，请求 model 为 `gpt-4o-mini`，返回 model 为 **`gpt-4o-mini-2024-07-18`**。API usage 为 11 input / 2 output / 13 total tokens。按用户给出的未折扣人民币单价计算约 ¥0.00000285；实际扣费以中转站账单为准。脱敏证据在 `artifacts/model-diagnostic-once/report.json`。

使用 OpenAI Docs 核对官方页面 https://developers.openai.com/api/docs/models/gpt-4o-mini ，Snapshots 节明确列出 `gpt-4o-mini` 和 `gpt-4o-mini-2024-07-18`。该返回标识属于官方 GPT-4o mini 的固定版本；原联调 guard 将别名与快照逐字比较，存在误拦截缺陷。本次已直接观察到该快照返回；首次响应 model 未保存，不能补称已恢复首次响应。

已在 reproduction/real_minimal.py 修正为明确映射：请求仍固定 gpt-4o-mini，允许返回 gpt-4o-mini 或已核对的 gpt-4o-mini-2024-07-18。其他模型、未知日期后缀和缺失字段仍拒绝，不使用 startswith 等宽泛放行。Embedding 仍要求精确 text-embedding-v4。报告对通过白名单的实际返回模型留痕，数字 usage 在模型校验失败前也保留。8 项纯内存 guard 测试通过，无付费调用。未修改 AMI 算法或 prompts。

累计真实调用：**LLM 2 次，Embedding 1 次**；累计 Add 1 次失败尝试，Search 0 次。两次 one-shot lock 均保留；本次排查没有重跑完整链路。当前模型标识兼容问题已修复，完整 Add/Search 真实复现仍未完成。

用户另提供了第一次请求的中转站账单：571 input / 29 output tokens，¥0.000052，标注 0.5x；该数据属于用户后台证据，不是首次 harness 自己采集的 usage。其扣费与用户给出的单价及 0.5x 倍率一致。余额不是本次中断原因。

## 完整链路成功：用户再次明确授权后的唯一一次完成尝试

在用户“额度范围内就直接跑”及随后“从当前状态继续”的明确指示下，继续了同一个已启动进程，没有重复初始化、重发请求或重跑已完成工作。使用新的固定目录 `artifacts/real-minimal-completion-once/` 与独立自造数据数据库；先前失败和诊断的报告、数据库、one-shot lock 全部保留。

入口读取历史计数 LLM 2 / Embedding 1，严格将本轮余量设为 LLM 3 / Embedding 4，使总量仍不超过各 5 次。新固定完成目录有独立 one-shot lock，不能自动重跑。保护逻辑的 9 项纯内存测试全部通过。

| 项目 | 成功结果 |
|---|---|
| 实际调用方式 | 真实 AMI startup + ASGI POST 路由，真实 OpenAI SDK/provider 调用；没有另外启动公网服务 |
| 输入 | 单条自造英文偏好 “My favorite fruit is mango.”；独立 synthetic IDs 与固定自造时间戳；无 AML evaluation data |
| Add | **1 次，HTTP 200**；success=true；request_id/user_id/session_id 精确回显 |
| 持久化 | SQLite 中 raw=1、fact=1、requests=1；正常 Add 完成后立即可检索 |
| Search | **1 次，HTTP 200**；query 为自造偏好问题、top_k=100；返回 2 条合法 memory，包含 mango |
| 本轮 LLM | **3 次**，全 HTTP 200；事实抽取、更新检测、recall；返回 model 均为已确认的 gpt-4o-mini-2024-07-18 |
| 本轮 Embedding | **3 次**，全 HTTP 200；启动预热、Add、Search；返回 text-embedding-v4，1024 维与向量校验通过 |
| 本轮 LLM tokens | input **1022**，output **66** |
| 本轮 Embedding tokens | **76**（含预热 3） |
| 历史累计 API 次数 | **LLM 5 / Embedding 4**，没有超出原各 5 次上限 |
| 异常与重试 | 无本轮 provider 错误、模型误判或契约失败；**0 次重试**；真实调用现已停止 |

费用仅按用户给定人民币单价估算，非自动读取账单：LLM `1022×0.15/1M + 66×0.60/1M = ¥0.0001929`；embedding `76×0.5/1M = ¥0.000038`；本轮合计 **¥0.0002309**。若中转站的 LLM 0.5x 倍率仍适用，合计约 **¥0.00013445**。实际扣费以供应商账单与舍入为准。

结果：原文+事实、模型调用、同步持久化、ID echo、即时召回、response schema 和 top_k 上限在这条最小真实路径上通过，未发现新增配置或 contract 问题。此前发现的模型 alias/snapshot 校验误判已修复且本轮通过。真实更新替换/历史问答、并发、压力、失败恢复、benchmark 准确率、Full 规模和正式官方接入不在本轮证据范围；用户隔离/幂等主要依据之前的 fake 工程测试。该结论不升级为官方认证。

报告：`artifacts/real-minimal-completion-once/report.json`，保存脱敏请求数、状态、模型标识、token 数、持久化计数与成本估算；不保存真实密钥、认证头或完整 provider 响应。所有模型凭据仍只在私密配置/进程环境使用；没有 Git push、部署、benchmark、申请提交。原 AMI 105 文件 hash 再次校验未改。

## 2026-10-07：队友交付包与 GitHub 准备

已整理 reproduction-v1.1 本地版本与 TEAM_HANDOFF.md，更新空凭据配置模板及申请草稿。压缩包从已审查的 Git commit 导出，只包含源码/文档/测试/模板；不含私密 .env、密钥、.venv、.git、数据库和运行日志。当前私密配置没有改动，没有新增模型请求。

本机未安装 gh，GH_TOKEN/GITHUB_TOKEN 未配置，尚无 GitHub origin；因此没有创建远端或 push。GitHub 创建/上传说明位于 TEAM_HANDOFF.md。项目负责人完成仓库归属、可见性与账号授权后才能执行远端动作；申请材料仍需真实公开 URL、固定 commit 和评测服务信息。
