# 项目角色与权限分工

应用只有普通用户（`user`）和管理员（`admin`）两个角色。服务端为每次请求识别调用者。开发、测试、发布是工程职责。代码仓库写权限、操作系统管理员身份、工作区管理员身份以及 App 的 `CAN MANAGE` 均不会自动授予应用管理员角色。

| 应用操作 | 普通用户 | 应用管理员 |
| --- | --- | --- |
| 发起调用、续聊、审批工具、写入会话和记忆 | 以本人身份操作 | 同样以本人身份操作 |
| 查询会话和记忆 | 仅本人 | 可按目标用户查询 |
| 查询调用结果、回放事件 | 仅有可信本人归属的调用 | 可查询其他用户及缺少可信归属的旧调用 |
| 查看与触发绑定的记忆流水线 | 禁止 | 允许，仍需云端流水线及底层授权 |

## 登录与授权配置

- **云端**由 Databricks Apps 入口验证用户登录和 App 访问权，并将认证身份转给后端。部署应保持后端只经受信的 Apps 入口访问。
- **本地**工作台从启动服务的 Databricks CLI profile 获取工作区用户名。直接打开 `http://localhost:8000/` 即可使用。同一实例的浏览器共用该身份及其应用角色，适用于本机单用户开发。如果授权过期，在终端运行 `databricks auth login --profile <profile>`，通过 Databricks 网页完成授权后刷新工作台。API 客户端也可显式发送 Bearer token，以该令牌所属用户的身份访问。
- **管理员名单**通过服务端环境变量 `AGENT_ADMIN_USERS` 提供，值为用逗号分隔的工作区用户名，与已认证用户名精确匹配。例：`AGENT_ADMIN_USERS=admin1@example.org,admin2@example.org`。本地在未跟踪的 `.env` 中设置后重启；云端在 App 部署环境中设置后重新启动。默认所有已认证用户均为普通用户。
- **查询接口**支持管理员在会话列表、会话历史、记忆列表中指定查询参数 `actor`，记忆搜索在请求体中指定 `actor`。已知托管会话或调用 ID 时可直接读取。配置请求用户工具后，管理员查询云端目标用户的私有命名空间还可提供 `principal_id`（目标用户的 Apps 转发用户 ID）。查询目标不会改变发起调用或写入时的身份。

## 工程职责与平台权限

开发人员用自己的应用账号验收；测试管理员功能时，需要单独授权测试身份。开发者提交版本、模型配置和测试结果，有发布权限的工程人员或 CI 完成部署。下列平台、资源和本机权限由各自管理范围决定。[部署步骤](DEPLOYMENT.zh-CN.md)

## 平台运维权限的作用域

| 管理范围 | 负责的操作 | 权限边界 |
| --- | --- | --- |
| 本机管理员 | 管理操作系统账号、进程、目录权限，按需处理备份或文件所有权。 | 日常运行由启动服务的操作系统用户读写三个 SQLite 文件。本机管理员身份只在本机生效。 |
| Databricks 工作区管理员 | 管理工作区设置、预览功能与工作区范围内的 Store 授权。 | 工作区管理员可以授予 Store 访问权；直接读取库内数据仍需有效的 `READ` 或 `WRITE`。账号级管理权和 Postgres 数据库角色另行核验。 |
| 云端资源管理员或所有者 | App 管理者维护目标 App 的共享名单。具备相应授权能力的模型、实验、Store 或 Lakebase 管理者分别管理各自资源。 | App 的 `CAN MANAGE` 作用于该 App。Unity Catalog 模型授权、MLflow 实验 ACL、Lakebase 项目管理权和 Postgres 库内角色各有独立范围。恢复共享 Lakebase 项目属于资源管理操作。 |

App 的 `CAN USE` 与 `CAN MANAGE` 控制应用入口和管理操作。Store 的 `READ` 与 `WRITE` 覆盖整库，`WRITE` 包含读取。Store 创建者与工作区管理员有权管理 Store 显式授权。[App 权限](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/permissions) · [Store 权限](https://docs.databricks.com/aws/en/agents/agent-memory/store-permissions)

## 逐项核对资源与角色

普通用户登录云端 App 后，模型和托管存储的调用由后端发出。下表中的“App 服务主体”是云端程序使用的非人员身份。个人直接打开实验、查询 Store 或连接数据库时，平台会另行检查这个人的权限。云端列描述部署需要的权限，实际授权以目标资源的 ACL 为准。

### 应用入口与云端资源

| 资源与操作 | 谁认证、以谁的权限执行 | 对应角色与授权 |
| --- | --- | --- |
| 云端 App 的聊天页面、调用 API、结果查询和事件回放 | 浏览器用户用 Databricks 登录，直接调用 API 的客户端使用其获授权身份的凭据。平台先检查 App 入口权限。 | **普通用户**需要目标 App 的 `CAN USE`。**App 管理者**用 `CAN MANAGE` 管理共享。访问某条会话、结果或事件还涉及该记录的归属，见数据归属说明。[App 权限](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/permissions) |
| Unity Gateway 模型列表与推理 | 本地后端用**开发者**启动服务时选定的 Databricks profile；云端后端用 **App 服务主体**。浏览器通过 App 接口取可见列表和默认模型。 | 调用模型的身份需要 `system` 上的 `USE CATALOG`、`system.ai` 上的 `USE SCHEMA` 和目标模型上的 `EXECUTE`。列表可见性和执行权限分别判定。**普通用户**在页面选模型只使用 App 入口权限；**模型资源管理者**管理授权。[模型权限](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/unity-gateway) |
| Session Store，会话历史与检查点 | 云端由 **App 服务主体**对绑定的会话库读写，需要该库的 `WRITE`。 | **普通用户**通过 App 续聊。**开发者**直接排查测试库时按需取得 `READ`，修改数据需要 `WRITE`。**Store 创建者或工作区管理员**管理授权。[Store 权限](https://docs.databricks.com/aws/en/agents/agent-memory/store-permissions) |
| Memory Store，跨会话记忆 | 云端由 **App 服务主体**对绑定的记忆库读写，需要该库的 `WRITE`。 | **普通用户**通过 App 记忆或召回。个人直查需该库的 `READ`，写入需 `WRITE`。这两种授权都覆盖整库；仅使用“我的记忆”的人无需获授整库权限。[Store 权限](https://docs.databricks.com/aws/en/agents/agent-memory/store-permissions) |
| Runtime Store，调用状态、结果与事件 | 云端运行时使用部署注入的 Lakebase 连接与数据库角色读写运行表。 | **普通用户**通过 App 查询结果、回放事件。**开发者**可用调用 API 验收。人员直连排障需另行取得连接及相应 Postgres 对象权限；**Lakebase 项目管理者**管理项目生命周期，**数据库角色管理者**管理库内授权。[项目部署说明](DEPLOYMENT.zh-CN.md) |
| 托管记忆流水线 | 工作台后端用 **App 服务主体**查询或触发流水线。云端需要同时绑定两类 Store，触发已有流水线还需要相应服务访问权。 | 由**应用管理员**触发；运行可能改写记忆。[工作台说明](CHAT_APP.md) |
| MLflow 云端追踪实验 | **App 服务主体**以 `CAN_EDIT` 写追踪。人员点击实验链接后，MLflow 独立校验其 Databricks 身份。 | **开发者**查看实验需要 `CAN_READ`；**实验管理者**修改实验 ACL 需要 `CAN_MANAGE`。**普通用户**聊天无需实验权限。[实验 ACL](https://docs.databricks.com/aws/en/security/auth/access-control#mlflow-experiment-acls) |
| 工作区部署目录、App 配置与资源绑定 | **获发布授权的开发者或 CI 身份**认证后上传源码、创建或更新 App 并绑定资源。首次创建与更新已有 App 的授权范围见部署指南。 | 发布身份需要写目标目录和操作目标 App 的权限；**云端资源管理者**处理所需授权，**工作区管理员**处理工作区级功能开通。**普通用户**的入口授权不含发布权限。[部署准备](DEPLOYMENT.zh-CN.md) |

### 本地开发、源码与工具

| 资源与操作 | 谁需要认证或授权 | 在本项目中的边界 |
| --- | --- | --- |
| 本地工作台与 Databricks profile | **开发者**使用本机账号启动服务，并为后端配置有效的工作区登录身份。 | 本地模型调用和工作台浏览器默认使用启动服务的 profile。应用按该工作区用户名检查数据归属与管理员名单。 |
| 本地 SQLite 与 MLflow | 默认由**开发者的本机进程**读写。三个 SQLite 文件按操作系统目录和文件权限访问，本地追踪写到开发用 MLflow 服务。工作台仅向应用管理员显示 Traces 入口，点击后打开当前会话的追踪。本机可直接访问独立的 MLflow 页面，无需应用管理员登录。 | 浏览器经本地后端使用会话、记忆和运行记录，无需数据库账号。**本机管理员**只在处理本机账号、文件权限或备份时介入。[存储配置](agent/storage.toml) |
| GitHub 公共仓库、开发分支 | 公开源码可匿名读取。**开发者**推送需 GitHub 认证和目标仓库写权限，并遵守分支规则。 | **仓库管理员**管理协作者和分支规则。GitHub 权限只控制代码协作。 |
| 内置工具及人工确认 | 普通用户可在聊天中使用 `get_current_time`。`send_message` 是演示工具，执行前由发起该操作的人确认。 | 两者均未接入外部账号。人工确认只针对一次工具动作，不产生云端资源授权。[工具实现](agent/tools/send_message.py) |
| MCP 网页搜索 | `system.ai.web_search` 使用 `auth = "app"`。本地使用启动服务的 CLI profile，部署后使用目标 App 的服务主体。 | 普通用户和应用管理员均可在聊天中搜索公开资料。工具只发送搜索词，外发内容应限于公开信息。资源管理者核对调用身份在 `system`、`system.ai` 和目标 MCP Service 上分别有 `USE CATALOG`、`USE SCHEMA`、`EXECUTE`。[官方搜索要求](https://docs.databricks.com/aws/en/machine-learning/model-serving/web-search#web-search-through-mcp) |

## 数据归属与旧记录

服务端将已认证用户名作为调用的 actor，并将所有者写入 Runtime Store 的请求信封。前台、后台、流式、重试、状态查询和事件回放共用同一份归属。后台重启后仍可校验。升级前的调用没有可信所有者，保留原记录并仅允许管理员读取。已有 SQLite 检查点和记忆仍按原 actor 分区；历史自定义 actor 可由管理员查阅，系统不会将其自动转移给个人。

托管会话按 Store 返回的所有者校验。SQLite 和内存检查点按用户与会话联合寻址。数据权限在 App 边界执行；直接持有 Store、Postgres、MLflow 或本机文件权限的人员，仍按相应资源的 ACL 访问。[服务端实现](runtime/server.py) · [工作台路由](runtime/ui.py)
