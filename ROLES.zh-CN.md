# 项目角色与权限分工

普通用户使用 Agent，开发者维护和验证软件，管理员在获授权的范围内管理资源和访问权。同一个人可以兼任多个角色，具体操作按当时使用的身份、目标资源和授权判断。下面的分工是项目的职责约定，实际强制执行的边界见末节。

## 谁负责什么

| 人员角色 | 在本项目中的职责 | 完成职责所需的权限 |
| --- | --- | --- |
| 普通用户 | 在聊天界面提问、选择可用模型、继续自己的会话、使用自己的记忆，确认与自己请求有关的工具操作。 | 云端通过登录身份取得目标 App 的 `CAN USE`。按当前调用方式，日常聊天只需应用入口权限，无需仓库写权限、模型直调权限或整库读取权。工具确认只针对当次动作。 |
| 开发者 | 修改 `develop` 上的代码和配置，维护提示词、工具和模型选择。用测试数据验证会话续聊、跨会话记忆、调用结果和事件回放，准备发布和验收。 | 提交代码需要仓库写权限。本地启动服务需要项目目录和 SQLite 文件的相应读写权限。本地直调云模型还需要开发用 Databricks 身份对目标模型有调用权。日常开发无需本机提权或工作区管理员身份。 |
| 管理员 | 管理资源生命周期和授权，处理跨用户数据访问、共享资源故障和权限审计。 | 每项权限都有作用域，见下表。管理本机、管理一个 App、管理工作区和管理数据库分别授权。 |

**发布如何交接。** 开发者提交版本、目标模型、Store 绑定和验收结果。首次部署由有权创建资源并完成授权的发布人或 CI 身份执行；更新已有 App 需目标 App 的 `CAN MANAGE` 及发布涉及的源码目录和资源权限。开发者经单独授权也可以担任发布人。普通用户用 `CAN USE` 验收聊天流程。[部署步骤与所需权限](DEPLOYMENT.zh-CN.md)

## 管理员的作用域

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
| 云端 App 的聊天页面、调用 API、结果查询和事件回放 | 浏览器用户用 Databricks 登录，直接调用 API 的客户端使用其获授权身份的凭据。平台先检查 App 入口权限。 | **普通用户**需要目标 App 的 `CAN USE`。**App 管理者**用 `CAN MANAGE` 管理共享。访问某条会话、结果或事件还涉及该记录的归属，见末节。[App 权限](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/permissions) |
| Unity Gateway 模型列表与推理 | 本地后端用**开发者**启动服务时选定的 Databricks profile；云端后端用 **App 服务主体**。浏览器通过 App 接口取可见列表和默认模型。 | 调用模型的身份需要 `system` 上的 `USE CATALOG`、`system.ai` 上的 `USE SCHEMA` 和目标模型上的 `EXECUTE`。列表可见性和执行权限分别判定。**普通用户**在页面选模型只使用 App 入口权限；**模型资源管理者**管理授权。[模型权限](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/unity-gateway) |
| Session Store，会话历史与检查点 | 云端由 **App 服务主体**对绑定的会话库读写，需要该库的 `WRITE`。 | **普通用户**通过 App 续聊。**开发者**直接排查测试库时按需取得 `READ`，修改数据需要 `WRITE`。**Store 创建者或工作区管理员**管理授权。[Store 权限](https://docs.databricks.com/aws/en/agents/agent-memory/store-permissions) |
| Memory Store，跨会话记忆 | 云端由 **App 服务主体**对绑定的记忆库读写，需要该库的 `WRITE`。 | **普通用户**通过 App 记忆或召回。个人直查需该库的 `READ`，写入需 `WRITE`。这两种授权都覆盖整库；仅使用“我的记忆”的人无需获授整库权限。[Store 权限](https://docs.databricks.com/aws/en/agents/agent-memory/store-permissions) |
| Runtime Store，调用状态、结果与事件 | 云端运行时使用部署注入的 Lakebase 连接与数据库角色读写运行表。 | **普通用户**通过 App 查询结果、回放事件。**开发者**可用调用 API 验收。人员直连排障需另行取得连接及相应 Postgres 对象权限；**Lakebase 项目管理者**管理项目生命周期，**数据库角色管理者**管理库内授权。[项目部署说明](DEPLOYMENT.zh-CN.md) |
| 托管记忆流水线 | 工作台后端用 **App 服务主体**查询或触发流水线。云端需要同时绑定两类 Store，触发已有流水线还需要相应服务访问权。 | 按职责由**获授权的云端运维人员**触发，**开发者**在测试环境验收。触发可能改写记忆。当前路由的角色检查见末节。[工作台说明](CHAT_APP.md) |
| MLflow 云端追踪实验 | **App 服务主体**以 `CAN_EDIT` 写追踪。人员点击实验链接后，MLflow 独立校验其 Databricks 身份。 | **开发者**查看实验需要 `CAN_READ`；**实验管理者**修改实验 ACL 需要 `CAN_MANAGE`。**普通用户**聊天无需实验权限。[实验 ACL](https://docs.databricks.com/aws/en/security/auth/access-control#mlflow-experiment-acls) |
| 工作区部署目录、App 配置与资源绑定 | **获发布授权的开发者或 CI 身份**认证后上传源码、创建或更新 App 并绑定资源。首次创建与更新已有 App 的授权范围见部署指南。 | 发布身份需要写目标目录和操作目标 App 的权限；**云端资源管理者**处理所需授权，**工作区管理员**处理工作区级功能开通。**普通用户**的入口授权不含发布权限。[部署准备](DEPLOYMENT.zh-CN.md) |

### 本地开发、源码与工具

| 资源与操作 | 谁需要认证或授权 | 在本项目中的边界 |
| --- | --- | --- |
| 本地工作台与 Databricks profile | **开发者**使用本机账号启动服务，并为后端配置有效的工作区登录身份。 | 本地模型调用使用这个 profile。开发页面显示的邮箱不能单独证明每个访问者都经历了独立登录。 |
| 本地 SQLite 与 MLflow | 默认由**开发者的本机进程**读写。三个 SQLite 文件按操作系统目录和文件权限访问，本地追踪写到开发用 MLflow 服务。 | 浏览器经本地后端使用会话、记忆和运行记录，无需数据库账号。**本机管理员**只在处理本机账号、文件权限或备份时介入。[存储配置](agent/storage.toml) |
| GitHub 公共仓库、开发分支 | 公开源码可匿名读取。**开发者**推送需 GitHub 认证和目标仓库写权限，并遵守分支规则。 | **仓库管理员**管理协作者和分支规则。GitHub 权限只控制代码协作。 |
| 内置工具及人工确认 | 普通用户可在聊天中使用 `get_current_time`。`send_message` 是演示工具，执行前由发起该操作的人确认。 | 两者均未接入外部账号。人工确认只针对一次工具动作，不产生云端资源授权。[工具实现](agent/tools/send_message.py) |
| 可选外部工具，如 MCP、Genie | 当前清单未绑定外部工具。后续若配置 `auth = "user"`，调用者需提供请求用户授权，并具备目标资源权限；若配置 `auth = "app"`，由应用运行身份访问。 | **开发者**声明认证方式，**目标资源管理者**授予相应身份所需权限。普通用户是否需要目标资源的个人权限取决于该工具的绑定方式。[资源清单](agent.toml) · [请求用户授权](README.md#request-user-authorization) |

## 当前实现能强制执行哪些边界

云端 App ACL 控制谁能进入应用。本机 SQLite 依赖操作系统文件权限。项目的 `actor` 和 `session_id` 用于归集记忆与会话，单凭这些字段不能证明调用者有权查看对应记录。[运行时契约](AGENTKIT_CONTRACT.md)

当前演示工作台支持在托管 Store 中按指定 actor 浏览和搜索记忆，也提供触发记忆流水线的操作。这些服务端路由未按上述三种人员角色逐项鉴权。因此，“普通用户只处理自己的数据”和“流水线由运维人员触发”是职责边界；面向多用户开放这些功能时，需要在服务端校验登录身份、数据归属和操作权限。对需要独立隔离的数据域，托管 Store 可按安全边界拆分。[工作台路由](runtime/ui.py) · [Store 权限](https://docs.databricks.com/aws/en/agents/agent-memory/store-permissions)
