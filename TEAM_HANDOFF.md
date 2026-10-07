# 队友接手与 GitHub 发布

交付版本：`reproduction-v1.1`（2026-10-07）。这是 ActiveMemoryIndex 的复现，不声明原创算法贡献。原作者、MIT LICENSE、prior work 和具体改动见 ATTRIBUTION.md / CHANGELOG.md。

## 压缩包内容

压缩包只包含已审查并提交的源码、文档、测试和空凭据模板；不含 `.env`、API Key、服务口令、.venv、.git、SQLite 数据库、模型数据、artifacts 或运行日志。队友自行建立 Python 环境、使用自己的密钥及服务口令。不要直接压缩整个服务器工作目录来替代这个交付包。

## 解压后先运行无费用测试

```bash
cd ami-reproduction
python3.10 -m venv .venv
.venv/bin/python -m pip install -r requirements-academic.lock.txt
.venv/bin/python -m reproduction.run_tests
.venv/bin/python -m reproduction.check_http
.venv/bin/python -m unittest reproduction.test_real_minimal reproduction.test_native_embedding -v
```

依赖安装需可用 PyPI 镜像；已有 Conda Python 3.10 也可用来创建 venv。fake HTTP 检查会自动启动/停止本地服务，无模型费用。Python 3.13 不适合本项目固定的 NumPy 版本。

`.env.example` 已更新为当前 Beijing/OpenAI-compatible 协议及零重试；WorkspaceId、API keys、服务口令必须由队友自行填写。`.env.academic.example` 是保留的 upstream 原始模板，不能混淆为本机配置。`AMI_AUTH_TOKEN` 为自行生成的服务口令，不是比赛方签发的 AML Key。

不要直接执行付费的 `real_minimal` / `diagnose_model` 或 academic 启动命令：它们会消耗模型配额，且当前已授权测试已完成。交付包不含本地 attempt.lock，因此不应把包中没有 lock 视为已获新的调用授权。

## 当前验证边界

- 26 个 upstream 测试文件通过；37 个 reproduction HTTP 检查和38个 upstream 本地 HTTP 检查通过。
- 一次完整真实 synthetic Add/Search 通过：本轮 LLM 3、Embedding 3 次；历史累计 5/4 次。
- 官方模型 snapshot 名称误判已修复；8个旧 guard 测试加1个历史限额测试，共9项通过。
- 没有 benchmark 成绩、官方 Smoke/Full 或 Evaluation Access Request 提交；不声称复现榜单分数。
- 原服务API、prompts、表示、fusion、ordering、neighbour和updates来自 upstream，具体复用须继续披露。

## 创建 GitHub 空仓库

本服务器未安装 gh，未配置 GitHub API token，也尚无 origin。本阶段不假装已经创建远端。

1. 登录 https://github.com/new ，Owner 选本人/团队；建议名称 `ami-reproduction`。
2. 准备公开提交开源方法材料时选择 Public；如先内部协作则选 Private，申请公开材料前再公开。公开动作应由项目负责人确认。
3. 不勾选自动生成 README、.gitignore 或 LICENSE：本地已包含这些文件。
4. 创建后复制 repository URL。不要向聊天或仓库发送 GitHub token。

在服务器原仓库中，经确认公开/推送且配置好本人 GitHub SSH 或 credential manager 后：

```bash
cd /data1/sp/Agent-Memory/ami-reproduction
git remote add origin https://github.com/YOUR_OWNER/ami-reproduction.git
git push -u origin main
git push origin reproduction-v1 reproduction-v1.1
```

若此前已设置 origin，先检查 `git remote -v`，不要盲目覆盖。GitHub 认证使用本机授权流程，不把 token 嵌入 URL 或命令历史。

队友仅有 ZIP 时可 `git init -b main` / `git add .` / `git commit` 后推送，但这会形成新的 Git 历史；建议由服务器原仓库首次 push，队友再 git clone，这样保留原始提交和固定版本。

GitHub 官方说明：
- https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository
- https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github

## 申请评测 Key 前仍需补齐

使用已更新的 EVALUATION_ACCESS_DRAFT.md：补公开仓库 URL、固定 commit、实际 Add/Search/Health HTTPS 地址、鉴权交付方式和运行容量信息。Quick Tunnel 公网连通性由用户独立验证，但不等于已冻结一个供评测长期使用的服务地址。完成表单内容后由项目负责人明确授权正式提交。
