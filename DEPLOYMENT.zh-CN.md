# 从本地 SQLite 切换到 Databricks 托管存储

在本项目中，保留默认的 `mode = "auto"` 即可部署。同一份代码在 `agentbricks dev` 下使用本地 SQLite，在 Databricks Apps 中使用部署注入的托管存储连接。**切换后端只改变后续读写的位置。** 已有本地会话、记忆、调用结果和事件仍留在原 SQLite 文件中；如需把历史数据带入云端，应单独安排迁移。

下面的命令适用于项目根目录、Bash 和 Agent Bricks CLI 0.10.0。第 1～3 节用于部署准备，第 4 节才会创建或更新云端资源，第 5 节会调用模型并写入专用测试数据。

## 1. 核对目标资源

下表对应当前分支的配置。若目标环境使用其他名称，先更新配置，再同步调整后续命令中的变量。[存储模式](agent/storage.toml) · [托管资源清单](agent.toml) · [应用启动命令](app.yaml)

| 资源 | 本地默认值 | 云端目标与用途 |
| --- | --- | --- |
| Session Store | `.agentbricks/local_agent_sessions.db` | `clinical-research-agent-eikyyo-sessions`。保存对话检查点和待审批状态。 |
| Memory Store | `.agentbricks/local_agent_memory.db` | `clinical-research-agent-eikyyo-memory`。按用户保存跨会话事实。清单填写显示名，部署时解析为 Store ID。 |
| Runtime Store | `.agentbricks/local_agent_runtime.db` | Agent Bricks 为该部署在 Databricks 管理的 Lakebase 项目中配置专用数据库，保存调用请求、状态、结果和有序事件。当前部署路径由平台选择底层项目。 |
| Databricks App | 本地工作台 | 部署短名 `clinical-r-eikyyo`，完整 App 名 `agent-bricks-clinical-r-eikyyo`。同一工作区重用该名称会更新已有 App。 |
| 模型与追踪 | 工作区模型、本地 MLflow | 当前默认模型为 `system.ai.claude-sonnet-5-5`，工作区追踪实验为 `/Shared/agentbricks_traces/clinical-research-agent-eikyyo`。 |

部署时，Agent Bricks 会创建缺少的已声明存储、复用已有存储，并为 App 身份配置访问权限。部署清单中的 Session Store 名、Memory Store 显示名均不能填入 Lakebase 项目 ID 或 SQLite 路径。[Agent Server 运行状态](https://docs.databricks.com/aws/en/agents/deploy/agent-server#run-state) · [Agent Bricks CLI](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli)

## 2. 按身份准备权限

普通用户、开发者、本机与云端管理员的职责，以及 App 运行身份与发布身份的区别，见[项目角色与权限分工](ROLES.zh-CN.md)。

| 身份 | 部署前核对 |
| --- | --- |
| 工作区管理员 | 确认目标工作区支持 Databricks Apps、托管 Lakebase 和所用 Unity Gateway 模型。在 **Previews** 中启用 **Agent Memory Sessions**。 |
| 执行部署的用户或服务主体 | 有权创建 App，或对同名 App 有 `CAN MANAGE`；可以向目标工作区目录同步源码。能创建清单中的会话库、记忆库，或管理已有库的授权；能在目标路径创建 MLflow 实验，或编辑已有实验并完成 App 所需授权。 |
| App 服务主体 | 运行时用自己的身份访问资源。两个托管 Store 需要 `WRITE`（包含读取），追踪实验需要 `CAN_EDIT`。部署流程配置 Runtime Store 连接与访问。在 Unity Gateway 核对模型服务的有效 `EXECUTE` 以及上级的 `USE CATALOG`、`USE SCHEMA`；现有继承授权也有效。 |
| 调用或验收 App 的人 | 使用目标工作区 OAuth 身份，具备 App 的 `CAN USE` 或更高权限。若还需直接检查实验或 Store，则分别需要实验读取权或对应 Store 的 `READ`。[App 权限](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/permissions) |

Store 创建者和工作区管理员可管理显式授权。核对 App 授权时使用服务主体的 **application/client ID**。Store 权限覆盖整库，`input.actor` 用于组织记忆；需要隔离不同安全域时使用独立 Store。[部署权限](https://docs.databricks.com/aws/en/agents/deploy/#permissions-to-deploy-an-agent) · [Store 权限](https://docs.databricks.com/aws/en/agents/agent-memory/store-permissions) · [模型权限](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/unity-gateway)

## 3. 确认配置并执行预检

在 `agent/storage.toml` 中选择一种模式，修改后重启本地服务或重新部署以生效。

| `mode` | 适用环境 |
| --- | --- |
| `auto` | 推荐共用此配置。本地默认三个 SQLite 库；若显式配置 `AGENT_SESSION_STORE` 或 `AGENT_MEMORY_STORE`，对应库优先走托管服务。云端由 AgentKit 选择后端。 |
| `cloud` | 适合只部署到云端的发布配置。启动时要求存在持久化 Runtime Store 连接；还要在 `agent.toml` 保留会话、记忆绑定。 |
| `local` | 强制三类存储都使用 `[sqlite]` 的路径。部署前改回 `auto` 或 `cloud`。 |

保留 `app.yaml` 的 `uv run start-server`。云端的 `AGENT_SESSION_STORE`（库名）、`AGENT_MEMORY_STORE`（解析后的 ID）和 Runtime Store 连接由部署流程注入。`.env`、SQLite 数据文件和本地开发生成的 `app.agentbricksdev.yaml` 留在本机。

先用以下只读命令确认分支、依赖和登录目标。把 `target-workspace` 换成自己已登录的 CLI profile；后续代码块沿用同一终端中的变量。项目要求 Python 3.11 或以上，验收示例另用到 `jq` 和 `uuidgen`。首次准备环境时，按官方步骤安装 Databricks CLI、`uv` 和 Agent Bricks CLI，并用 `databricks auth login` 为目标工作区建立 OAuth profile。[安装与登录](https://docs.databricks.com/aws/en/agents/quickstart#prerequisites)

```bash
PROFILE=target-workspace
DEPLOY_NAME=clinical-r-eikyyo
APP="agent-bricks-${DEPLOY_NAME}"
SESSION_STORE=clinical-research-agent-eikyyo-sessions
EXPERIMENT=/Shared/agentbricks_traces/clinical-research-agent-eikyyo

git branch --show-current
uv lock --check
agentbricks --version
agentbricks doctor .
databricks auth profiles
databricks current-user me --profile "$PROFILE"
```

预期分支为 `develop`，`doctor` 的项目接入检查全部通过。对照 `auth profiles` 的主机确认工作区；`current-user me` 返回的是之后执行部署的身份。

本项目此前遇到过共享 Lakebase 项目 `databricks-internal-custom-agents` 处于软删除状态，导致两个托管 Store 创建失败。若使用同一工作区，可在部署前只读核查当前状态；若仍有 `delete_time`，由工作区管理员确认原删除原因和处理方式后再继续。更换 Store 名无法解决这个底层状态问题。

```bash
databricks postgres get-project projects/databricks-internal-custom-agents \
  --profile "$PROFILE" -o json | jq '{name, delete_time, purge_time}'
```

## 4. 部署时执行

从项目根目录发布。首次部署会分配云端资源；若清单中的 App 名已存在，先确认它就是本次更新的目标。

```bash
agentbricks --profile "$PROFILE" deploy "$DEPLOY_NAME" --source . --no-wait
agentbricks --profile "$PROFILE" deployments wait "$APP"
agentbricks --profile "$PROFILE" deployments get "$APP"
```

`wait` 退出码为 `0` 后再验收；`75` 表示仍在部署，重跑同一条 `wait` 即可。其他失败先看 `deployments get` 及下列日志，定位资源或授权错误。部署详情中的 `url` 是应用地址。

```bash
agentbricks --profile "$PROFILE" deployments logs "$APP"
```

## 5. 在云端逐项验收

以下调用通过 App 名和 profile 完成认证。模型测试使用已登录的测试账号和随机代码。每次新调用使用新的顶层 `id`；同一对话复用顶层 `session_id`；跨对话记忆复用同一登录身份。请求格式见[项目运行时契约](AGENTKIT_CONTRACT.md)。

### 确认后端和 App 授权

```bash
api() { agentbricks --profile "$PROFILE" -o json endpoint invoke "$APP" "$@"; }
A=$(uuidgen)
B=$(uuidgen)
api --method GET --path /api/ui/config --query "session_id=$A" |
  jq '.body | {deployed, session, memory, streaming, tracing}'
```

预期 `deployed=true`、`session.managed=true`，会话库名与清单一致；`memory.enabled=true` 且 `memory.store` 为 `memory-stores/<ID>`；`streaming.persistent=true` 且 `streaming.mode` 为 `Runtime Store`。这些字段确认应用选择了云端后端，实际读写用下面的调用验证。

如需审计授权，从上一步的 `memory.store` 取完整资源名。以下命令由 Store 创建者或工作区管理员执行，核对 App 服务主体的显式授权。权限子命令 `get` 只返回当前 CLI 调用者的有效权限。

```bash
MEMORY_RESOURCE=$(api --method GET --path /api/ui/config --query "session_id=$A" |
  jq -er '.body.memory.store')
agentbricks --profile "$PROFILE" sessions stores permissions list "$SESSION_STORE" --all
agentbricks --profile "$PROFILE" memory stores permissions list "$MEMORY_RESOURCE" --all
```

### 验证会话和跨会话记忆

```bash
turn() {
  local body
  body=$(jq -nc --arg id "$1" --arg sid "$2" --arg msg "$3" \
    '{id:$id, session_id:$sid, input:{messages:[{role:"user", content:$msg}]}}')
  api --path /api/invocations --json "$body"
}

CHAT_CODE=$(uuidgen)
MEM_CODE=$(uuidgen)
turn "$(uuidgen)" "$A" "本轮对话的临时代号是 $CHAT_CODE。仅回复收到，不要使用记忆工具。"
turn "$(uuidgen)" "$A" "只根据本轮对话，回答刚才的临时代号，不要调用记忆工具。"
turn "$(uuidgen)" "$A" "请用 remember 将我的笔记本编号 $MEM_CODE 保存到长期记忆。"
RESULT_ID=$(uuidgen)
turn "$RESULT_ID" "$B" "请用 recall 查找我的笔记本编号，只回复编号。"
```

确认每次返回 `body.status=completed`。会话 A 的第二个回答应包含 `CHAT_CODE`，且没有通过记忆工具检索；会话 B 的提问没有携带编号或会话 A 的历史，回答应包含 `MEM_CODE`。检查 `body.output.output` 中的工具调用，确认写入和读取分别执行了 `remember`、`recall`。

### 验证调用结果、事件回放和追踪

```bash
RUN_PATH="/api/invocations/$RESULT_ID"
api --method GET --path "$RUN_PATH" | jq '.body | {id, status, output}'
EVENTS=$(api --method GET --path "$RUN_PATH/events" --sse | jq -ce '.events')
printf '%s\n' "$EVENTS" | jq 'map({id, event})'

CURSOR=$(printf '%s\n' "$EVENTS" | jq -r '.[0].id')
api --method GET --path "$RUN_PATH/events" --query "after=$CURSOR" --sse |
  jq -e --argjson all "$EVENTS" '(.events // []) == $all[1:]'

agentbricks --profile "$PROFILE" tracing list --experiment-name "$EXPERIMENT" --limit 10
```

原调用 ID 应能读到相同结果。事件 ID 按序递增，以 `run.completed` 结束；游标比较应输出 `true`。在绑定的 MLflow 实验中，以时间和会话 B 关联刚才的追踪。若要验证**跨进程持久性**，在一次计划内的 App 重启或同名重新部署后，沿用原 `RESULT_ID`、`EVENTS` 和相同身份，再次 GET 结果与事件；应返回原结果和相同事件序列。无需为此删除部署或重建存储。

## 常见问题与回到本地开发

| 现象 | 核对位置 |
| --- | --- |
| 启动时报缺少持久化 Runtime Store | 检查是否把 `cloud` 模式用于本地启动；在云端检查部署日志中的 Runtime Store 配置、资源挂载和 App 身份。 |
| App 可打开，调用模型或存储却返回 403 | 先看哪个资源拒绝了哪个身份。App 调用模型和 Store 使用服务主体；验收人员直接列出 Store 授权或查看实验使用自己的 profile。 |
| 同一会话能续聊，新会话找不到记忆 | 核对两次请求使用相同的可信 `input.actor`、不同的 `session_id`，并检查第一轮是否真正调用 `remember`。省略 actor 时服务端使用已认证用户名。 |
| 新环境查不到以前的结果 | 核对 profile、完整 App 名、调用 ID 和调用身份。切换环境后，本地保存的调用 ID 仍属于原本地数据库。 |

回到本地时，把 `agent/storage.toml` 设回 `auto` 或 `local`，从原项目目录执行 `agentbricks dev`。使用原 `[sqlite]` 路径即可继续读取已有本地数据。云端绑定继续保存在 `agent.toml`。
