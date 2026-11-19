# Clinical Research Agent

A LangGraph agent served by `databricks_agentkit.DurableAgentServer`. During `agentbricks dev`, the Runtime Store saves requests, results,
status and ordered events in `.agentbricks/local_agent_runtime.db`. After restarting in the same
project directory, use the original invocation ID to fetch its result or replay its event stream.
Deployment uses the managed Lakebase Runtime Store.

The generated project separates portable agent execution from the managed HTTP protocol:

```text
client -> runtime/main.py -> runtime/adapter.py -> agent/agent.py:run_agent
```

- `agent/agent.py` owns the framework-native agent, sessions, tools, MCP lifetime, and `run_agent`.
- `runtime/adapter.py` owns the agent-author integration hooks: runtime input/output translation plus
  `invoke` and `recover`.
- `runtime/main.py` constructs the server and registers those hooks.

To bring an existing LangGraph agent, run `agentbricks init --framework langgraph --existing .` in its
project and follow the generated prompt in your coding agent. The migration skill and this template share
[AGENTKIT_CONTRACT.md](AGENTKIT_CONTRACT.md), which owns integration requirements. This README owns
configuration and client examples; [AGENTS.md](AGENTS.md) provides the development map.

## Model configuration

Set `default_model` under `[agent]` in [agent.toml](agent.toml) and restart the server to apply
the change. The chat UI gets the effective default from the server. A bound model service
(`AGENT_MODEL_SERVICE_AGENT`) takes precedence, and a request can select a model with `input.model`.

Set `use_responses_api = true` in the model's table in [agent.toml](agent.toml) to use the
Responses API. For example, `[models."system.ai.gpt-6-astra"]` enables tool calls with reasoning
for Astra when its flag is true. Quote the full model service name so TOML preserves the dots.
A missing flag or table defaults to Chat Completions. The agent reads the setting for the
selected endpoint each time it builds a graph.

## Storage profiles

[中文部署指南：从本地 SQLite 切换到云端托管存储](DEPLOYMENT.zh-CN.md)

Set `mode` in [agent/storage.toml](agent/storage.toml) and restart the server after a change.

| Mode | Session, memory and runtime storage |
| --- | --- |
| `auto` (default) | `agentbricks dev` uses SQLite. An explicitly configured `AGENT_SESSION_STORE` or `AGENT_MEMORY_STORE` takes precedence for that store. Deployment uses AgentKit. |
| `local` | All three stores use the paths in the `[sqlite]` table. Run from the project directory with development dependencies installed. |
| `cloud` | AgentKit uses deployment-provided connections. Startup requires a configured persistent Runtime Store. |

The default SQLite files live under the ignored `.agentbricks/` directory. Point a path at an
existing database to reuse its data. Changing profiles selects storage for subsequent runs;
data migration is a separate operation. For cloud deployment, set the managed session and memory
bindings in [agent.toml](agent.toml). Agent Bricks provisions and connects the Lakebase Runtime
Store on deployment. The default `auto` profile works in both environments.

## Run locally

```bash
agentbricks dev
```

The API is available at `http://localhost:8000/api/invocations`, or on the port you pass with
`--app-port`. Requests can fail for a few seconds while the server starts; wait for
`Uvicorn running on ...` in the log.

The local workbench uses the Databricks identity of the CLI profile that started the server.
Open `http://localhost:8000/` directly. Every browser using this loopback instance shares that
identity. To renew workspace authorization in the browser, run
`databricks auth login --profile <profile>` for the same profile, then refresh the workbench.
Local API clients can also supply a Bearer token to act as its workspace user.
Set the optional server environment variable `AGENT_ADMIN_USERS` to a comma-separated list of
workspace user names to grant cross-user reads and memory pipeline access. See [project roles](ROLES.zh-CN.md).

Every invocation supplies a UUID `id`.
That ID is the invocation identifier and idempotency key. Supply the stable application session as
the top-level `session_id`; agent-specific values live inside the opaque `input` object:

```bash
SESSION_ID=$(uuidgen)
INVOCATION_ID=$(uuidgen)

curl -sS http://localhost:8000/api/invocations \
  -H 'Content-Type: application/json' \
  -d "{\"id\":\"$INVOCATION_ID\",\"session_id\":\"$SESSION_ID\",\"input\":{\"messages\":[{\"role\":\"user\",\"content\":\"What time is it? Use your tool.\"}]}}"
```

Reuse `SESSION_ID` for multi-turn conversation state. Generate a new `INVOCATION_ID` for each turn.
Retrying the same request with the same invocation ID returns the persisted result; changing the
request while reusing the ID returns `409`.

### 本地目录技能

在本机启动 Docker，准备配置中的 Python 镜像，然后从项目根目录启动工作台。

```bash
docker pull python:3.12.10-slim
agentbricks --profile ws-poc dev
```

把可信技能放在 `skills/<技能名>/`，目录内保留原始 `SKILL.md`、引用文件和脚本。正文的 YAML 头包含与目录名一致的 `name` 和用于自然语言匹配的 `description`。`agent/skills.toml` 配置技能根目录、镜像、命令超时和会话文件保留天数。工作台可直接输入“用文献检索技能为 KRAS G12C 肺癌生成 quick 检索计划，使用 dry-run 并保存 JSON”。

Agent 先读取正文，按需阅读引用文件，然后用参数数组运行包内 Python 或 Bash 入口。执行目录为 `/work`，通过 `save_files` 发布输出。返回的下载链接仅对当前用户开放。会话下一轮可列出并恢复文件，默认保留 30 天。每次请求首次执行命令时才创建容器，同次请求复用，结束时删除。无需 Docker 即可浏览技能说明。

容器使用本机 Docker 的默认网络，可访问匿名文献 API；实际可达性取决于开发机网络。仅安装可信包。沙箱以非 root 用户运行，技能和本次指定的输入只读挂载。第三方 API 密钥代发另行接入。云端部署使用已有托管工具绑定。

运行本地 Docker 集成测试：

```bash
TEST_SKILL_DOCKER=1 uv run pytest -q tests/test_local_skills.py
```

## Long-term memory

The Session Store keeps one conversation's state, keyed by the top-level `session_id`. The Memory Store
keeps facts across conversations under the authenticated workspace user. Omit `input.actor` or
supply your own user name. The server verifies ownership for invocation results and event replay.

During `agentbricks dev`, the memory tools save facts in `.agentbricks/local_agent_memory.db`.
Recall searches keywords in the current actor's paths, descriptions and facts. In the local
workbench, ask the agent to remember a fact, start a new session and ask for the fact again.
Use the same authenticated identity across the two sessions. The same check against
a deployed app uses the bound Databricks Memory Store:

```bash
# Conversation 1: the agent saves a fact with its memory tool.
agentbricks --profile <profile> endpoint invoke agent-bricks-<name> --path /api/invocations \
  --json "{\"id\":\"$(uuidgen)\",\"session_id\":\"$(uuidgen)\",\"input\":{\"messages\":[{\"role\":\"user\",\"content\":\"Remember that I prefer answers as bullet points.\"}]}}"

# Conversation 2: a new session_id, same actor, so the agent can recall it.
agentbricks --profile <profile> endpoint invoke agent-bricks-<name> --path /api/invocations \
  --json "{\"id\":\"$(uuidgen)\",\"session_id\":\"$(uuidgen)\",\"input\":{\"messages\":[{\"role\":\"user\",\"content\":\"How do I like my answers formatted?\"}]}}"
```

## Invocation modes

- Foreground: omit `background` and `stream`; the response contains the agent result under `output`.
- Foreground streaming: set `stream: true`; the response is SSE backed by persisted events.
- Background: set `background: true`; poll the returned `status_url`.
- Background streaming: set both flags; the `202` response includes `status_url` and `events_url`.

```bash
INVOCATION_ID=$(uuidgen)
curl -sN http://localhost:8000/api/invocations \
  -H 'Content-Type: application/json' \
  -d "{\"id\":\"$INVOCATION_ID\",\"session_id\":\"$SESSION_ID\",\"input\":{\"messages\":[{\"role\":\"user\",\"content\":\"Count to three.\"}]},\"stream\":true}"

INVOCATION_ID=$(uuidgen)
curl -sS http://localhost:8000/api/invocations \
  -H 'Content-Type: application/json' \
  -d "{\"id\":\"$INVOCATION_ID\",\"session_id\":\"$SESSION_ID\",\"input\":{\"messages\":[{\"role\":\"user\",\"content\":\"Summarize durable agents.\"}]},\"background\":true}" | jq
curl -sS "http://localhost:8000/api/invocations/$INVOCATION_ID" | jq
```

SSE records contain events translated by `runtime/adapter.py`: token `delta`s, completed `message`s,
and HITL `interrupt`s. Replay from a cursor with
`GET /api/invocations/{id}/events?after={sequence}`.

A message's `content` is its answer text. Models that reason (Claude 5.x) add a separate
`{"role": "assistant", "content": [{"type": "reasoning", ...}]}` message before the answer.

## Human approval

`send_message` is gated by `HumanInTheLoopMiddleware`. Start a turn asking the agent to use that
tool. When the output or event stream contains an `interrupt`, submit a new invocation with the same
application session:

```json
{
  "id": "<new-uuid>",
  "session_id": "<same-session-id>",
  "input": {
    "resume": {"decisions": [{"type": "approve"}]}
  }
}
```

For OBO tools (`auth = "user"`), the same signed-in user submits the approval as a new
invocation. Keep the same `session_id` and, if supplied, `input.actor`. The new request supplies
its own user credentials; the paused invocation's credentials are discarded. The browser normally
uses its existing authenticated session, so approval does not require a page refresh or another
login unless that session has expired. Automatic crash recovery still cannot recover OBO work.

Malformed middleware decisions return `INVALID_APPROVAL` before the resume value is checkpointed,
so the user can correct the decision and submit it with a new invocation ID.

During `agentbricks dev`, SQLite saves conversation checkpoints and pending approvals in
`.agentbricks/local_agent_sessions.db`. Reuse the same session ID and actor after restarting the
server to continue a conversation. The browser remembers its current session ID. Invocation
status and event replay use the separate local SQLite Runtime Store. To persist graph checkpoints
on Databricks, bind a managed Session Store before deploying:

```bash
agentbricks sessions bind my-agent-sessions
```

## Crash recovery

Deployment with a Runtime Store supports invocation recovery after worker loss. Graph checkpoint
persistence requires a Session Store separately. Recovery can replay work, so tools must tolerate
repeated side effects. See [Recovery and durability](AGENTKIT_CONTRACT.md#recovery-and-durability).

## Chat app

The browser UI is included by default. It generates a stable application session ID in local
storage, sends it as the top-level `session_id`, and generates a fresh invocation UUID per turn.
Use `agentbricks init --framework langgraph --disable-chat-app` for API-only output.

## Configure and deploy

- Change the model, instructions, tools, graph, and framework-native execution in `agent/agent.py`.
- Change `runtime/adapter.py` only to map a different application input/output contract.
- Add local tools under `agent/tools/`; modules are auto-discovered.
- Add MCP servers in `agent/mcps.py` or with `agentbricks tools add mcp`.
- Bind long-term memory with `agentbricks memory bind <store>`; `agentbricks deploy` creates the
  store if it doesn't exist. Local development uses SQLite for memory and session checkpoints by default. To start with
  memories already in the store, create it and add entries before you bind and deploy:

  ```bash
  agentbricks memory stores create --name <store>    # prints a Store ID
  agentbricks memory entries create --store <Store ID> --actor-id you@example.com \
    --path /preferences/style.md --content "Prefers concise answers."
  ```

  `--path` is the entry's key in the store, not a file on your machine; the deployed chat app uses
  the signed-in user's email as the actor.
- Route the agent's model through a Unity Gateway model service you own with
  `agentbricks experimental models bind`, then search for a better model and prompt on your own
  eval set with `agentbricks experimental models upgrade`. These commands are experimental, so
  their flags may change between releases.

```bash
agentbricks --profile <profile> deploy agent-langgraph --source .
```

When deployment provisions a dedicated Runtime Store, only the app-owned
`databricks_agentkit_runtime_<hash>` schema and runtime tables are added. Managed Runtime Store
deployments use their own default schema.

To keep a session on one app replica (sticky routing), set the `X-Routing-Key` request header to your
stable session id on every call. It is a routing hint only: non-blank, no more than 128 UTF-8 bytes,
used verbatim, and it takes precedence over the legacy `__Host-databricks-app-router` cookie. It is not
authentication and is never used as the application's session id. Send that separately as the
top-level `session_id` in the request body.

# Request-user authorization

Declared tools in `agent.toml` select `auth = "user"` or `auth = "app"`; legacy missing auth
continues to use the application/default identity. Deployed user tools require the request resolver
and never fall back to application credentials. Model, memory-service, session-service, and custom
MCP server credentials are unchanged.

`runtime/main.py` derives the invocation policy after `configure()`. The runtime adapter namespaces
public session IDs and actor values for the request owner, then passes only `workspace_client_for`
to the framework-native agent. Internal session keys are never returned to clients.

User-policy invocations use the existing Runtime for synchronous, streaming, and background calls,
including status polling, event replay, and invocation-ID idempotency. The Runtime Store persists no
credential; request-user authentication remains process-local for the active first attempt. A
replacement attempt fails with `MCP_USER_AUTH_RECOVERY_UNSUPPORTED` before agent code runs.
Interactive approval uses a new authenticated invocation and rebuilds tools with that request's
credentials. The agent's existing namespaced memory, conversation store, and checkpointer behavior
is unchanged; OBO does not add another saver.

Existing generated projects must update their copies of `agent/agent.py` and `runtime/adapter.py`
to remove the OBO pause/resume rejection guards. Upgrading the installed package alone does not
rewrite these files. Keep the runtime's automatic-recovery guard.
