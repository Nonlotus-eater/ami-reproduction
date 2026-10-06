# Reproduction status

更新：2026-10-06，Phase 3 / reproduction-v1。

**结论：无需付费 API 的本地 reproduction baseline 已完成。** fake 工程链路通过，不等于真实 academic 质量复现，也不是官方 contract 认证。当前没有服务持续监听；测试服务已自动停止。未提交任何外部材料。

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
