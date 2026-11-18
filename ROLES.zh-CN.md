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

## 请求发出后，用谁的身份

| 访问对象 | 本地开发 | 云端部署 |
| --- | --- | --- |
| 模型 | 后端用开发者启动服务时配置的 Databricks profile 调用模型。浏览器向后端取模型列表并发送所选模型。 | 后端用 App 服务主体调用模型。该主体需要上级目录的 `USE CATALOG`、上级 schema 的 `USE SCHEMA` 和目标模型服务的 `EXECUTE`。[模型权限](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/unity-gateway) |
| 会话、长期记忆、调用结果与事件 | 默认由本机服务进程读写 Session、Memory、Runtime 三个 SQLite 文件。 | App 服务主体需要对 Session 和 Memory Store 有 `WRITE`。Runtime Store 使用部署提供的数据库连接及相应访问权。[存储配置](agent/storage.toml) |
| 追踪 | 默认写入开发环境的本地 MLflow。 | App 服务主体向绑定的实验写入追踪，需要实验的 `CAN_EDIT`。人员直接查看或管理实验时按自己的实验 ACL 判断。[资源清单](agent.toml) |

App 服务主体是程序运行身份。发布人负责把程序部署上去，普通用户发起业务请求，两者的个人凭据均无需交给生产 App。

## 当前实现能强制执行哪些边界

云端 App ACL 控制谁能进入应用。本机 SQLite 依赖操作系统文件权限。项目的 `actor` 和 `session_id` 用于归集记忆与会话，单凭这些字段不能证明调用者有权查看对应记录。[运行时契约](AGENTKIT_CONTRACT.md)

当前演示工作台支持在托管 Store 中按指定 actor 浏览和搜索记忆，也提供触发记忆流水线的操作。这些服务端路由未按上述三种人员角色逐项鉴权。因此，“普通用户只处理自己的数据”和“流水线由运维人员触发”是职责边界；面向多用户开放这些功能时，需要在服务端校验登录身份、数据归属和操作权限。对需要独立隔离的数据域，托管 Store 可按安全边界拆分。[工作台路由](runtime/ui.py) · [Store 权限](https://docs.databricks.com/aws/en/agents/agent-memory/store-permissions)
