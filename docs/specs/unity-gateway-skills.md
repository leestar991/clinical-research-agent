# Skills 与会话沙箱开发规格

状态为重新设计，日期为 2026-11-19。基线为 `develop@3023af5`，Agent 框架为 LangGraph 和 `DurableAgentServer`，首个验收对象为 `life-sciences-literature-search`。

## 1. 目标与环境边界

Agent 先发现技能、阅读 `SKILL.md`，再按正文读取参考文件、选择包内 CLI 和参数，在当前用户的会话沙箱中执行。检索规划、数据源选择、去重、证据结构及导出格式由技能定义。应用负责提供文件、分配执行环境、落实权限和交付产物。技能在已有权限内增加 CLI 选项或导出格式后，开发者应只需更新技能包。

| 能力 | 本地开发 | Databricks 云端部署 |
| --- | --- | --- |
| 发现及加载技能 | 从配置的本机目录列举技能，直接读取正文和引用文件 | App 身份通过 Unity Gateway Skills MCP 发现并读取目标 UC schema 内获准的技能 |
| 提供完整包 | 取本地目录的一致性快照，校验后只读挂载到本地沙箱 | 由 App 从 UC 技能对象取得同一版本的完整文件树，校验后交给云端会话沙箱 |
| 运行脚本 | 会话专属本地容器，在容器内调用原生 CLI | Agent Bricks 绑定 `system.ai.sandbox`，通过 MCP 在该会话的远端沙箱里运行原生 CLI |
| 计算生命周期 | 每次会话执行首次需要运行代码时创建容器，本次执行结束即销毁 | 按会话、按需创建。连续空闲 2 小时停止，连续空闲 24 小时销毁 |
| 会话文件 | 保存在沙箱外的本机私有会话目录，默认 30 天 | 保存在沙箱外的云端私有会话目录，默认 30 天 |
| 身份与存储 | 浏览器沿用工作台认证；沙箱不继承个人 CLI、家目录或宿主 .env。会话、记忆及调用状态沿用本地存储配置 | 平台调用使用 App 服务主体；聊天用户决定会话和产物归属。会话、记忆及调用状态沿用云端托管配置 |

本地模式的**技能读取和代码执行**应能在没有 UC、Skills MCP 和云端 Sandbox 权限时工作。已有模型、Web Search 及真实文献来源各有自己的网络依赖。云端模式遇到权限或文件错误时给出相应错误，避免跨环境寻找同名技能而掩盖部署问题。

## 2. 已核实的平台行为

| 项目 | 依据与对设计的影响 |
| --- | --- |
| Unity Gateway Skills | UC 中的技能以 `catalog.schema.skill` 命名。Skills MCP 端点为 `https://<workspace-host>/ai-gateway/skills/?schema=<catalog>.<schema>`。其职责是发现、加载和取得文件。此前 ws-poc 的握手记录包含 `list_skills`、`load_skill`、`get_skill_files`。实施时以 App 自身的 MCP 工具清单核对契约。正文加载成功仅表明取得了说明。[Skills 概览](https://docs.databricks.com/aws/en/agents/uc-skills) · [发现与使用](https://docs.databricks.com/aws/en/agents/uc-skills/use-uc-skills) |
| 完整技能包 | 官方 Files API 使用 `/Skills/<catalog>/<schema>/<skill>/` 保存正文、脚本及附属文件。发布流程包括创建、上传、Finalize；元数据 API 提供 etag。执行侧仍须检查文件齐全和内容摘要，且并发发布时不得混用两次读取的内容。[Skills API](https://docs.databricks.com/aws/en/ai-gateway/skill-reference) |
| Sandbox MCP | `system.ai.sandbox` 提供 `create_sandbox`、`run_code`、`delete_sandbox`。首次调用 `run_code` 可隐式建沙箱；后续须复用返回的 `sandbox_id` 才能复用文件。支持 Python 和 shell（sh）；云端网络及运行时安装依赖的能力与工作区配置有关。[执行代码](https://docs.databricks.com/aws/en/agents/custom-agents/code-interpreter-tools) |
| Agent Bricks 绑定 | 本机 CLI 与项目虚拟环境均为 0.10.0。在临时目录复制基线 agent.toml，实际执行下方命令，确认其效果是写入托管工具声明。项目 `agent/agent.py` 已调用 `databricks_agentkit.langgraph.mcp_tools(..., workspace_client_for=...)`。安装版 SDK 根据 binding 建立 MCP 客户端，并在调用时注入受保护的沙箱元数据。[Agent Bricks CLI](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli) · [Agent Server 授权](https://docs.databricks.com/aws/en/agents/deploy/agent-server) |

下列命令用于**云端部署源目录**。它配置沙箱工具；技能 schema、文件交接和应用内的会话归属仍由项目配置及适配层负责。

```bash
agentbricks tools add sandbox --auth app
```

本机 CLI 实测生成的等价 TOML 如下。本次仅在临时工程执行过该命令。

```toml
[[tools]]
id = "sandbox"
auth = "app"
source = { kind = "sandbox", service = "system.ai.sandbox" }
policy = { databricks_access_token_included = true }
```

`--auth` 缺省为 `user`。显式使用 `app` 后，上述默认策略还会让沙箱内的代码获得所选身份的 Databricks 凭据。CLI 另提供 `--no-databricks-access-token-included`，控制的是凭据是否提供给沙箱代码。安装版 SDK 的 `SANDBOX_DOWNSCOPE_ENABLED=False`，会拒绝非空的 `policy.downscope`。设计时按 App 的**实际有效权限**做隔离评估。CLI 写入成功、已有 Sandbox SDK 探测成功、Sandbox MCP 端到端执行成功，分别记录证据。

## 3. 项目配置与统一接口

本地实现使用项目自有的 `agent/skills.toml`。下例同时列出已实现的本地字段与规划中的云端配置，Agent Bricks 原生托管工具声明继续放在 `agent.toml`。

```toml
mode = "auto"

[local]
skill_provider = "directory"
directory = "skills"
image = "python:3.12.10-slim"
command_timeout_seconds = 240
sandbox_provider = "local_container"

[cloud]
skill_provider = "unity_gateway"
catalog = "poc_workspace"
schema = "agent_skills_dev"
approved_skills = ["life-sciences-literature-search"]
sandbox_provider = "databricks_mcp"
sandbox_binding = "sandbox"

[lifecycle.local]
destroy_on_invocation_end = true

[lifecycle.cloud]
idle_stop_hours = 2
idle_destroy_hours = 24

[session_files]
directory = ".agentbricks/skill-files"
retention_days = 30
session_deletion_cleanup_hours = 24
```

示例 schema 用于 ws-poc 验证。生产环境单独填写经授权的 catalog/schema。本地技能目录相对于项目根目录解析，云端制品不包含本机目录或私有 skill 源码。`auto` 复用项目已有的部署环境判定；启动时验证所选 provider、配置和运行环境一致。环境变量只承载本机路径覆盖或受信侧的凭据引用。

**托管清单也要按环境生效。** 当前 SDK 会从有效 `agent.toml` 自动发现声明的 MCP。建议本地工程保留基础清单；云端打包时从受版本控制的源生成独立部署目录，在其清单加入上述 sandbox binding，再以该目录作为 deploy 的 source。部署前比对生效清单。仅在 Python 中切换 provider、却让本地清单继续声明云端 sandbox，会触发错误的远端发现。两个环境继续使用各自所需的已有工具和 Store 绑定。

| 项目接口 | 共同契约 | 本地 / 云端实现 |
| --- | --- | --- |
| `SkillProvider` | `discover` 返回可见名称与简介；`load` 返回已核验正文及 SkillRef；`read_file` 按相对路径读取引用；`materialize` 提供已固定的完整文件树及清单 | 本地目录快照 / App 身份的 Skills MCP 加 UC Files API |
| `SandboxProvider` | `acquire` 按所有者、内部会话和本次执行取得沙箱。`stage` 交付指定 SkillRef 与所需会话文件，`exec` 运行原生 CLI，`collect` 发布文件。`release` 在本地销毁容器，在云端释放执行租约并开始计算空闲时间 | 本地容器运行时 / AgentKit 绑定的 Sandbox MCP，配合 App 后台生命周期管理 |
| 运行协调与产物存储 | 记录 owner、session、invocation、tool call、SkillRef 和沙箱代次；检查归属、并发、超时、重放和下载 | 本地私有目录与持久索引 / 云端私有存储与持久索引 |

SkillRef 至少含来源、逻辑名、包摘要以及可用的服务端版本信息。完整文件清单覆盖入口、模块及参考文件。由服务端解析实际路径、宿主身份、远端沙箱 ID 和权限元数据。进入模型上下文的参数保持通用，例如已加载技能句柄、包内相对入口、argv、工作文件和所需产物名称。普通聊天和阅读说明阶段都不创建沙箱。

## 4. 一次调用如何落地

1. **发现和阅读。** Agent 先阅读所选技能的真实 `SKILL.md`，再按需读取其中引用的文件。加载成功后，服务端签发与本次调用及包摘要关联的句柄。加载失败或摘要变化时，执行请求在创建沙箱前结束。
2. **固定包并交接。** 本地对配置目录生成快照，拒绝逃逸的符号链接和越界路径。云端先以 App 权限定位 UC 对象，枚举并取得完整文件树，核验清单，再把同一快照放入目标沙箱。不能从仅返回正文的 MCP 响应推断脚本已安装，也不能假定 Files API 的 `/Skills/...` 是沙箱内的 POSIX 挂载点。
3. **验证云端文件通道。** 优先实测当前工作区是否提供受支持、可只读访问的包路径。若没有，首版候选为 App 通过 Files API 取包，由服务端通过同一 sandbox ID 的 `run_code` 分块写入沙箱暂存目录，校验总长及摘要后原子激活。字节流不进入模型上下文或正文日志。MCP 消息大小、超时、读回及依赖可用性列为 P0 云端验证项。通道无法满足完整包时明确阻塞云端执行。
4. **执行和复用。** 选定技能后，Agent 根据说明决定是否运行 `--help`、检索、补查或导出。例如可运行包内 `scripts/search_literature.py`，再由 Agent 决定是否以 `scripts/render_evidence.py` 消费已有证据。两种 provider 均实际调用原生入口。云端可用固定 Python 引导程序在 `run_code` 中以参数数组启动 CLI，避免把问题拼成 shell 代码。固定包按调用使用只读快照或等效完整性校验，可写工作区按 invocation 分开。
5. **约束平台工具。** 云端复用 SDK 的声明、认证和受保护元数据。在统一工具接线中只暴露执行协调入口，避免另行暴露一组可接受任意 `sandbox_id` 的原始托管工具。服务端验证绑定后才把 ID 传给 MCP，普通用户和管理员均不可借此执行或删除他人沙箱。
6. **结果与恢复。** 记录退出状态、超时、来源状态和产生的文件；由技能解释证据与格式。发布产物时校验文件类型、大小与归属并生成独立快照。下载及会话重开均复核 owner。已完成的工具调用重放时返回原回执；远端提交后断线须先核对执行状态，状态不可判定时报告中断，避免重复代发。上游失败、零命中、缺凭据跳过要分别呈现。

## 5. 身份、第三方凭据与生命周期

| 主体 | 职责和权限 |
| --- | --- |
| 普通应用用户 | 登录工作台并操作自己的会话、记忆、调用和产物。云端平台请求由 App 服务主体发起。 |
| 应用管理员 | 沿用既有的跨用户管理查询、记忆流水线和 trace 入口权限。产物下载和沙箱执行仍校验所有者。开发与发布属于工程职责。 |
| App 服务主体 | 对目标 skill 持有父级 `USE CATALOG`、`USE SCHEMA` 及 skill 的 `READ VOLUME`；对 `system.ai.sandbox` 核查父级使用权和 MCP 的 `EXECUTE`。以实际 App 身份完成正反验证。ws-poc 已知业务 App client ID 为 `676444a0-d408-4b4d-9497-2c9a50ba55cb`。[使用技能的权限](https://docs.databricks.com/aws/en/agents/uc-skills/use-uc-skills) · [Databricks 托管 MCP](https://docs.databricks.com/aws/en/agents/mcp-tools/built-in-mcp-services) |
| 发布者 / 平台管理员 | 配置预览、发布与 Finalize、授予运行所需权限。App 运行身份只取得消费技能所需授权。[Skills API 权限表](https://docs.databricks.com/aws/en/ai-gateway/skill-reference) |

第三方来源的 API key 由受信代发组件保管。沙箱只能请求获准的操作，受信侧约束目的地、任务、额度和有效期，并处理重定向及日志脱敏。来源权限清单决定允许访问什么；`--source`、查询式、候选排序和导出由技能与 Agent 决定。对依赖环境变量判断供应商可用性的原生包，先验证受支持的 HTTP 适配接口；必要时只适配传输和“凭据已配置”信号，记录兼容性差异及包摘要。缺 key 时保留技能的跳过行为。Q5 继续待定，本版无内容筛选规则。

云端 App 身份是多个聊天用户共用的平台主体。若向沙箱提供该主体的 Databricks 凭据，沙箱代码可能直接调用 App 有权访问的资源。须分别验证应用 secrets、第三方凭据、共享 Store 和他人产物均不能被这条路径读取。第三方密钥保存在沙箱身份无权读取的受信边界。若业务 App 的必要授权与沙箱内任意代码隔离冲突，则该执行模式不能通过生产验收；评审可显式选择关闭沙箱内 token、由 App 在外部代办受控平台操作，并单列哪些沙箱内操作因此不再可用。控制面仍使用 App 身份。

### 5.1 本地沙箱按一次会话执行管理

一次会话执行指服务端接受一条用户请求后产生的一个 invocation。首次需要运行命令时立即创建专属容器，同一次执行中的 Bash、Python、检索和导出步骤共用它。执行进入成功、失败、取消或超时的终态时，协调器终止剩余进程并立即销毁容器及其临时卷。关闭浏览器只断开客户端，执行是否结束以服务端状态为准。

文件在步骤完成时按 §5.3 发布，最终收尾也须等待文件发布得到明确结果。写入失败时记录文件错误，不能向用户返回成功下载回执。无论收尾是否成功，都要释放计算资源。服务重启时按持久化的 invocation 归属和执行状态补收遗留容器。用户在同一会话继续提问时，按需创建新容器，装载所需技能和已保存的会话文件。

### 5.2 云端沙箱按需创建、停止与销毁

每个环境中的每个所有者和内部会话最多绑定一个有效云端沙箱。同会话的沙箱操作串行取得执行租约。应用持久记录 `sandbox_id`、代次、活动租约和 UTC 时间 `last_active_at`。空闲从最后一次实际执行及其文件交付结束、释放租约时起算。创建完成后尚未执行过命令的沙箱从就绪时起算。浏览历史、下载已发布文件、健康检查和后台扫描均不续期。

| 触发条件 | 应用动作 | 后续执行 |
| --- | --- | --- |
| 首次需要沙箱 | 用 App 身份通过 Sandbox MCP 创建，记录归属与代次 | 在绑定的 ID 上装载并运行技能 |
| 有活动执行 | 持有租约，保护命令和文件交付免受应用定时回收 | 后续请求等待当前执行释放租约 |
| 连续空闲满 2 小时，未满 24 小时 | 停止计算，确认平台状态为已停止，保留原 ID 与空闲起点 | 有新执行时启动同一沙箱，核实就绪后运行。执行完成后重新计时 |
| 连续空闲满 24 小时 | 删除原沙箱，确认删除后解除绑定。两个阈值都从同一个空闲起点计算 | 有新执行时创建新代次，仅取回本会话所需的有效文件 |

例如，周一 10:00 一次执行结束，此后没有新执行，周一 12:00 停止，周二 10:00 销毁。后台任务和用户请求都须在同一绑定上检查截止时间、租约与代次，避免请求重新使用已到销毁期限的沙箱。停止、启动与删除支持重试，状态以平台回读为准。应用重启后继续按原截止时间处理逾期项。失联租约先核实在途任务，再决定是否回收。

云端执行走 MCP。公开的 MCP 工具清单列出创建、执行、删除。应用后台可用同一 App 身份调用官方 Sandbox SDK 的 `get_sandbox`、`stop_sandbox`、`start_sandbox` 管理计算，并核实沙箱的原生空闲配置与 2 小时策略一致。上线前必须在 ws-poc 验证 MCP 返回的 ID 可由该 App 通过控制面管理，并观察真实状态转换。若无法保留原 ID 停止再恢复，云端生命周期验收未通过。平台另有每次启动后最多连续运行 24 小时的上限，可中断活动命令。协调器须记录中断，按已持久化的步骤回执恢复，避免重复执行有副作用的命令。[Sandbox MCP](https://docs.databricks.com/aws/en/agents/custom-agents/code-interpreter-tools) · [SDK 停止与重启](https://docs.databricks.com/aws/en/compute/serverless/sandbox-usage-guide) · [平台运行上限](https://docs.databricks.com/aws/en/compute/serverless/sandbox)

### 5.3 用户隔离的会话目录与 30 天保留期

用户上传的输入、供后续步骤或轮次使用的工作文件、最终导出均保存在沙箱外。每个目录使用服务端从登录身份及会话归属解析出的内部键，布局为 `<持久根目录>/<owner_key>/<session_key>/`。同名文件按不可变文件版本保存。索引记录相对路径、文件版本、来源 invocation、摘要、持久化时间和到期时间。临时缓存、依赖和未声明保留的进程临时文件随沙箱清理。

本地使用仅应用账户可读写的持久目录。云端使用受信文件服务管理的私有持久存储，可用相同前缀布局。列举、写入、下载及回装均先校验所有者与内部会话，只向沙箱交付本次所需文件。不得把整个用户目录根或共享存储凭据交给沙箱。拒绝绝对路径、目录穿越和逃逸的符号链接。沙箱停止、销毁或应用重启均保留已发布的文件。

每个文件版本从首次成功持久化的 UTC 时刻起保留 **30 天**。读取、下载、沙箱唤醒和重放均不延长有效期。到期即拒绝读取、下载与回装，后台可重试地清除文件和下载授权。用户主动删除会话时立即撤销访问，并在既定的 24 小时内完成文件清理。此时限从删除请求生效时起算。部署可调整云端空闲阈值和文件保留期，启动时须校验各期限为正且停止阈值小于销毁阈值。

## 6. 实施顺序和验收

| 顺序 | 交付与通过条件 |
| --- | --- |
| P0-1　接口和环境选择 | 同一 Agent 工作流可替换两组 provider。记录生效清单和 provider；本地 Skills/Sandbox 的调用日志无 UC、Skills MCP 或远端 Sandbox 请求。云端配置缺失时不读取本机目录。 |
| P0-2　本地端到端 | 在缺少 UC 与 Sandbox 权限的开发环境从工作台提交下方原题。事件依次证明读取本地正文、按需读取参考、执行包内 CLI、读回真实证据。同一 invocation 的两次命令使用同一容器，仅读说明时创建数为零。分别验证成功、失败、取消、超时后容器消失。同会话下一轮取得新容器并能读回上轮导出，其摘要一致。 |
| P0-3　云端接线与探针 | 用 App 身份在 ws-poc 读取获准 skill，并用已存在且未授权的 skill 做拒绝对照。通过绑定的 Sandbox MCP 实测创建、传包、校验、`--version`、跨调用文件复用、读回及删除。记录 App 标识、UC 全名、摘要和 MCP 工具版本或握手清单。 |
| P0-4　双端业务复验 | 云端工作台用同一问题及同一批准包再跑一次。至少验证 PubMed、Europe PMC 和一个确实需要有效 key 的操作。记录来源、稳定 PMID/DOI/NCT、上游失败及文件下载校验。匿名成功、错误 key 的 401 和模拟响应分别标注。 |
| P0-5　隔离与扩展 | 两个用户使用相同公开会话号仍得到不同沙箱，越权执行、读文件、下载均被拒绝。用真实测试凭据验证同一沙箱的 shell 与 Python 读不到第三方 key；同一用户换会话不会串文件。用原生 RIS 或 BibTeX 导出复用已有证据，应用无需新增导出分支。 |
| P0-6　云端生命周期 | 以可控时钟测空闲 119/120 分钟及 23:59/24:00 小时的阈值。另在 ws-poc 用 App 身份与缩短的测试期限实测两个场景：停止后同 ID 唤醒并重新计时，以及持续空闲到期后旧 ID 消失、下一轮新建且文件摘要一致。保存转换时间、脱敏 ID、平台状态及生效配置，核对部署默认值为 2/24 小时。 |
| P0-7　会话文件 | 两个用户使用相同展示会话号和文件名，同一用户在两个会话使用同名文件，均不能跨目录列举、读取或回装。在两端验证执行环境销毁后仍可下载。将时钟推进到每个版本的第 30 天到期前后，核对到期前可读、到期即拒绝、清理完成且相邻会话未受影响。 |
| P1　生产可靠性 | 模拟在途命令、收尾写入失败、停止与新调用竞争、服务重启及重复清理，核对租约、文件回执和最终平台状态。主动删除会话后立即撤销下载并在时限内清理。验证超时、上游 429、全部来源失败和重复提交。将错误与正常零命中分开。 |
| P2　发布与版本管理 | 在双端实检通过后补齐私有制品发布、晋级、回滚与历史包留存。首版可由授权发布者人工发布，运行 App 无需写权限。 |

工作台复验使用原始输入：

> 帮我对比一下一线 KRAS G12C 突变非小细胞肺癌在研/上市竞品的注册临床数据,重点看 PFS、ORR、DCR 这些疗效终点和安全性。

证据记录至少包含环境、会话及 invocation、所加载正文和完整包的摘要、实际沙箱代次、原生 CLI 入口与参数、退出状态、来源统计及下载校验。业务验收区分一线随机注册研究、早期队列及已上市产品的后线数据；注明 PFS、ORR、DCR 和安全性的分母、截止时间或缺报原因。公开检索结果随时间变化，双端对比依据执行链路、结构和稳定标识。
