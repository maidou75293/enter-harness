# enter-harness

> **企业级智能体运行时内核**（agent runtime kernel）。
> 把「模型能做什么」变成「企业敢让它在生产环境做什么」。

`enter-harness` 是一个 Python 库，不是 HTTP 服务。宿主工程通过 `pip install` 引入，
直接构造 `Agent` 运行任务；模型、存储、权限、事件、工具全部由宿主以端口（Port，
只定契约、不定厂商的接口）注入。业务代码留在宿主，内核只负责运行时治理。

> 命名：工程名、发行名、导入名统一为 `enter-harness` / `enter_harness`。
> 旧名 `agent-core` / `agent_core` 保留兼容层（re-export + 子模块别名），
> 老宿主不改代码也能继续用；新代码请统一写 `enter_harness`。

它不是演示框架，而是为**多租户、要审批、要审计、要恢复的生产系统**设计的：
模型可换、存储可换、权限可接，但隔离、审批、审计、恢复这些企业底线由内核统一保证。

![enter-harness 架构图](docs/architecture.svg)

> 一张图看懂：宿主只实现业务；身份、上下文、工具、状态、审计由内核统一治理。

```python
from enter_harness import Agent, AgentConfig, RunRequest, Scope

agent = Agent(AgentConfig(
    scope=Scope("tenant-1", "agent-1", "user-1", ("assistant",)),
    model=model,               # 真实模型：create_model(...) 或实现 ModelPort
    store=my_store,            # 实现 StorePort 的适配器
    tools=my_tools,            # ToolRegistry
))
result = agent.run(RunRequest(objective="分析本周销售数据"))
```

---

## 为什么适合企业级应用

| 企业要求 | enter-harness 的做法 |
|---|---|
| 多租户隔离 | `Scope` 贯穿全链路；记忆 / 知识 / 工具都按租户过滤，跨租户默认拒绝 |
| 权限与审批 | 工具三级可见性（租户 / 角色 / scope）+ `needs_approval` → 落单 → 幂等恢复 |
| 可审计 | 审计只追加、带哈希链；改任何历史记录 `verify_chain()` 立刻断链 |
| 记忆治理 | 六类记忆 + R1 唯一约束 + D1 确定性排序；规则块超限报错，绝不截断 |
| 可恢复 | 运行状态与检查点（start / tool / approval / done），失败可续跑 |
| 可横向扩展 | PostgreSQL 后端 + 部分唯一索引 + advisory lock，多副本并发写不撞车 |
| 可替换、不被绑定 | 模型、存储、权限、事件全部端口化；换模型 / 换库不改业务代码 |
| 可验证 | 366 项单元测试 + 三后端端到端（含审批闭环），发版有基线 |

---

## 记忆与上下文：企业级记忆管理

记忆不是「把聊天记录塞进 prompt」，而是**有身份、有范围、有权威、可审计的断言**。
`enter-harness` 把这件事做成一套统一模型：写入有唯一约束，召回有固定次序，
组装有预算和解释，换存储不改语义。

### 六类记忆，各回答一个问题

| 类型 | 回答什么 | 组装优先级 |
|---|---|---|
| `identity` 身份 | 这是谁、属于哪个租户 / 角色 | platform / tenant |
| `profile` 画像 | 用户与项目的稳定偏好 | user → project → platform |
| `semantic` 语义 | 稳定事实（库地址、口径、负责人） | project → platform → user |
| `procedural` 流程 | 必须遵守的规则与流程 | platform → project → user |
| `episodic` 情景 | 发生过什么（经验片段） | session → project |
| `working` 工作 | 当前任务的临时状态 | session |

一条记忆只表达一个可判断真假的断言，`key` 是它的稳定身份：同 key 新结论会
`supersede` 旧结论，内容等价则 `noop`，不会越写越多、越写越乱。

### 四条企业级保证

1. **R1 唯一性**：同租户 / 范围 / 智能体 / 类型 / key / 条件只能有一条 `active`。
   SQLite 用部分唯一索引，PostgreSQL 用部分唯一索引 + advisory lock，
   并发写不会出现「两个真相」；Markdown 后端靠文件级约束与人审保证。
2. **D1 确定性排序**：`procedural` 规则块永远排在事实之前，`project` 事实先于
   `user` 画像，与 `select_records` 使用同一套 `scope_rank(kind, scope)`；
   换执行器、换模型都不会改变记忆次序。
3. **规则不截断**：上下文按块做预算，超限整块丢弃；`procedural` 块超限直接抛
   `ContextOverflow`，绝不把「必须遵守的规则」切成半段。
4. **可解释、可观测、可迁移**：`assemble()` 返回 `trace / used_chars / dropped`，
   每个块为什么留下、为什么丢弃都有记录；`ContextMetrics` 可接监控；
   记忆与知识可在 SQLite / Markdown / PostgreSQL 之间迁移，审计链保持同库连续。

### 租户与隔离

```python
from enter_harness import MemoryRecord

ports.memory.upsert(MemoryRecord(
    tenant_id="t1", scope="project", scope_id="p1",
    kind="semantic", key="项目库地址", content="10.0.0.5:3306",
    source="user",
))
# 查询必须显式带 tenant_id；不带就只会查默认租户，不会跨租户
rows = ports.memory.query({"tenant_id": "t1", "scope": "project",
                           "scope_id": "p1"})
```

多租户 SaaS 里，这四条合起来意味着：**不同客户的数据不会串、同一事实不会分裂、
规则不会被裁掉、每一次注入都能复盘**。

详见 [核心模型：记忆 / 知识 / 上下文](docs/核心模型-记忆知识上下文-v1.md)。

---

## 为什么需要它

把模型直接接到业务系统上，通常会遇到同一组问题：

- 工具谁来管？谁能看见、能不能调、调用要不要审批？
- 上下文超长怎么办？哪些能截断、哪些绝对不能？
- 任务跑到一半失败，能不能从检查点恢复？
- 多租户、多智能体之间怎么隔离？
- 出了事怎么审计？谁在什么时候对什么做了什么？
- 换个产品，这套运行时能不能直接复用？

`enter-harness` 把这些问题收进内核，用统一端口和默认实现解决；
产品只实现自己的模型、存储和权限，不再各自重写一遍运行时。

---

## 核心能力

### 1. 内核与业务分离，换产品可复用

- 公开入口只有 `Agent.run / stream / start`（会话用 `Agent.session(...)`）；
- 模型、存储、权限、事件、工具全部端口化，不绑定厂商；
- `enter_harness/` 不反向依赖任何业务模块，可直接被多个宿主复用。

### 2. 10 个治理端口，覆盖 Agent 运行全链路

| 端口 | 职责 |
|---|---|
| `IdentityPort` | 身份事实，只读，不从对话推断 |
| `ContextPort` | 上下文块元数据与正文 |
| `MemoryPort` | 长期记忆查询 / 写入 / 归档 |
| `KnowledgePort` | 知识检索与回取 |
| `AccessPolicyPort` | 数据访问判定 |
| `IngestPort` | 用户输入 / 工具结果 / 事件采集 |
| `ActionPort` | 工具清单、调用、审批、恢复 |
| `StatePort` | 运行状态与检查点 |
| `VerifyPort` | 结果验收 |
| `AuditPort` | 审计哈希链与查询 |

`PortSet(...).check()` 在启动时校验端口齐全，并调用端口自检；
失败直接启动失败，而不是运行到一半才炸。

### 3. 三套存储后端，同一套契约

| 后端 | 记忆 / 知识 | 状态 / 审计 | 适用 |
|---|---|---|---|
| SQLite（默认） | SQLite | SQLite | 单机、开发、小数据 |
| Markdown | 人可读写的 `.md` | 仍落 SQLite | 人工维护知识 / 画像 |
| PostgreSQL | PostgreSQL | PostgreSQL | 多副本、服务端、并发写 |

```python
from enter_harness.harness import build_local_ports

ports = build_local_ports("/data/agent", backend="sqlite")      # 默认
ports = build_local_ports("/data/agent", backend="markdown")    # 人可读写
ports = build_local_ports("/data/agent", backend="postgres",    # 多副本
                          dsn="postgresql://user:pass@host:5432/db")
```

- PostgreSQL 可选依赖：`pip install 'enter-harness[postgres]'`；
- 记忆写入有 R1 唯一约束（同租户 / 范围 / key / 条件只能有一条 active），
  PostgreSQL 用部分唯一索引 + advisory lock 保证并发安全；
- Markdown 人工改正文会自动重算指纹；已审核知识被改后自动退回 `pending`；
- 三套后端之间没有自动迁移，切后端前先导出重放。

### 4. 三种执行器，按任务复杂度选择

- `ReactExecutor`：ReAct 子图，适合开放任务；
- `GraphExecutor`：Planner / Verifier + 图执行，适合可拆解任务；
- `HybridExecutor`：图 + 节点内 ReAct，预算耗尽时自动升级。

`Agent.start()` 返回后台句柄，支持取消、改向、审批恢复；
`StatePort` 记录 start / tool / approval / done 四类检查点。

### 5. 上下文预算：整块丢弃，关键内容不截断

- 按块做预算，超限整块丢弃，不输出半段；
- `procedural`（规范 / 规则）块超限直接抛 `ContextOverflow`，绝不截断；
- `assemble()` 返回 `text / blocks / trace / used_chars / dropped`，
  每个块为什么留下、为什么丢弃都可解释；
- `ContextMetrics` + `MetricsRegistry` 聚合注入块数、丢弃原因、缓存命中、
  预算利用率，便于接监控。

### 6. 工具治理：可见性、校验、审批、审计

- `list_tools(identity, scope)` 按租户 / 角色 / scope 过滤；
  声明了限制但身份不满足时**默认拒绝**；
- 直接按名字调用也会校验可见性，绕过清单不能绕过治理；
- `ToolSpec` 统一描述 `risk` / `side_effect` / `approval` / `danger` / `parallel`；
- 注册期校验工具名、JSON Schema、字段类型，坏工具启动即失败；
- 审批支持 `needs_approval` → 落单 → `resume`，同一审批单重复恢复不会重复执行；
- 工具调用、审批、验收、运行开始 / 结束全部可写审计；
- `action_mode="port"` 时，工具清单与执行改走 `ActionPort`：
  可见性过滤、策略 allow / ask / deny、审批单与审计统一由端口负责，
  默认 `legacy` 行为不变。

### 7. 审计与合规：只追加 + 哈希链

- 每条审计记录带前一条的哈希，改任何历史字段都会导致 `verify_chain()` 失败；
- 支持查询、留存天数、显式 `prune`；
- SQLite / PostgreSQL 两套实现；
- 审计失败不阻断主链路，但宿主应监控写入量与链校验结果。

### 8. 多租户 / 多智能体隔离

- `Scope(tenant_id, agent_id, user_id, role_ids)`；
- 同一智能体的不同会话 / 用户共享 agent 级记忆；不同智能体默认不共享；
- `AgentService` 提供租户下多智能体 CRUD、按智能体建 / 列会话；
- 记忆查询只有在请求里显式带 `agent_id` 时才按智能体隔离，避免误共享。

### 9. 可扩展，不锁死实现

- `ModelPort`：`complete / stream / count_tokens / embed`；
- provider 注册表：内置 OpenAI 兼容实现，可 `register_provider` 接入任意厂商，
  `create_model` 直接产出同步 `ModelPort`，可选用 `on_delta` 做流式；
- `StorePort` / `AgentStorePort`：记忆、事务、ledger、会话；
- `ToolPolicyPort`：工具调用前 `allow / ask / deny`；
- `EventSink`：start / model / node / done 事件；
- 宿主可以只实现自己关心的端口，其余用默认实现。

### 10. 可验证

- 366 项单元测试（公开仓基线；本地含旧宿主测试时为 371）；
- 存储端到端覆盖 SQLite / Markdown / PostgreSQL，并串起
  记忆 → 上下文 → 知识审核 → 检查点 → 审计链 → Agent 运行 → 审批闭环；
- PostgreSQL 用例在未设 `HARNESS_PG_DSN` 时自动跳过，不拖累本地开发；
- 版本号单一来源（`enter_harness/_version.py`）：`enter_harness.__version__`、
  `pyproject.toml`、已安装包的发行元数据三者同源，`tests/test_version.py` 守护；
  宿主可以直接用 `enter_harness.__version__` 打点，不必自己读 `importlib.metadata`。

---

## 工程化能力

### 工具目录分层

`ToolCatalog` 在治理型 `ToolRegistry` 之上按来源分层（`builtin` / `mcp` /
`skill` / `subagent` / `host`），支持注册期配额与来源统计：

```python
from enter_harness import ToolCatalog

catalog = ToolCatalog(quotas={"mcp": 20, "skill": 10})
catalog.register("search", handler, source="mcp")
catalog.counts()     # {"builtin": 0, "mcp": 1, ...}
catalog.summary()    # total / counts / quotas
```

配额是注册期限制；热更新先 `unregister` 再 `register`。

### 存储迁移

三套后端之间可以导出/导入记忆与知识（审计链和运行状态不迁移）：

```bash
python -m enter_harness.harness.migrate   --from-backend sqlite --from-root /data/a   --to-backend postgres --to-dsn "$HARNESS_PG_DSN"
```

```python
from enter_harness import build_local_ports, migrate_ports

src = build_local_ports("/data/a", backend="sqlite")
dst = build_local_ports("/data/b", backend="markdown")
print(migrate_ports(src, dst, tenant_id="t1"))
```

记忆按 R1 唯一约束幂等，知识按 id 覆盖并保留审核状态。

### 可观测性导出

`OtelEventSink` 把运行事件转成 OpenTelemetry span；不装 `opentelemetry-api`
也能 `import enter_harness`，只有真正创建 sink 时才要求依赖：

```python
from enter_harness import OtelEventSink

AgentConfig(..., events=OtelEventSink())   # span 名 agent.<event.type>
```

`pip install 'enter-harness[otel]'`。

### 任务级评测

`enter_harness.eval` 用一组用例跑 Agent 并给出通过率，支持超时与异常判定：

```python
from enter_harness.eval import EvalCase, run_cases

report = run_cases(
    [EvalCase("hello", "打个招呼", expect_contains=("你好",))],
    agent_factory=lambda case: build_agent(case),
)
print(report.passed, report.total, report.pass_rate)
```

离线自测：`python -m enter_harness.eval.runner --cases enter_harness/eval/cases.json`。

---

## 架构一眼看懂

一次运行的闭环：

```mermaid
flowchart LR
  A["定义<br/>IdentityPort"] --> B["采集<br/>IngestPort"]
  B --> C["上下文<br/>Context / Memory / Knowledge"]
  C --> D["控制<br/>ModelPort"]
  D --> E["动作<br/>Action / Policy / Approval"]
  E --> F["状态<br/>StatePort"]
  F --> G["验收<br/>VerifyPort"]
  G --> H["审计<br/>AuditPort"]
  H -.->|"审计 / 回流"| C
```

- 实线是主链路，虚线是审计与回流；
- 每个方块都对应一个端口，默认实现可替换；
- 存储层三套后端共用同一套契约，切换不改业务代码。

---

## 快速开始

要求 Python 3.10+。

```bash
# 从 GitHub 安装（带 PostgreSQL 后端）
pip install "enter-harness[postgres] @ git+https://github.com/maidou75293/enter-harness.git@v1.4.0"

# 或本地开发
pip install -e /path/to/enter-harness
```

### 接入真实大模型

内置 `openai` provider 走 OpenAI 兼容协议，OpenAI、DeepSeek、通义、vLLM、
Ollama、one-api 网关都适用；返回值已经是同步 `ModelPort`，直接交给 `Agent`：

```python
from enter_harness import Agent, AgentConfig, create_model

model = create_model(
    "openai",
    base_url="https://api.deepseek.com/v1",   # 任意 OpenAI 兼容地址
    api_key=os.environ["DEEPSEEK_API_KEY"],   # 不要把密钥写进代码
    name="deepseek-chat",
)
agent = Agent(AgentConfig(scope=..., model=model, store=..., tools=...))
```

流式（SSE / 逐字输出）只要传一个回调：

```python
model = create_model("openai", base_url=..., api_key=..., name=...,
                     on_delta=lambda text: send_to_client(text))
```

内置 `anthropic` provider 走 Anthropic 原生 Messages API（不是 OpenAI 兼容层）：

```python
model = create_model("anthropic", api_key=os.environ["ANTHROPIC_API_KEY"],
                     name="claude-sonnet-5-5")
```

消息里的 system / tool_use / tool_result 与工具 schema 会自动转换；流式同样支持
`on_delta`，结束帧带 usage 与 finish_reason。Anthropic 不提供 embedding，
`embed()` 会明确报错，需要时接 OpenAI 兼容嵌入或 `local_embed`。

- 环境变量缺省：`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`，
  `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`；
- 其他厂商或私有网关：`register_provider("my", factory)` 注册一个返回
  `async chat / complete` 对象的工厂，再用 `create_model("my", ...)`；
- 多套模型配置：`build_model(settings)` 按 `models.profiles` 里的
  `provider` 字段选择，返回同样的同步 `ModelPort`；
- 流式默认请求 `stream_options.include_usage`，结束时能拿到 `usage` 与
  `finish_reason`（个别网关不认就传 `stream_usage=False`）；
- 连接错误 / 408 / 429 / 5xx 默认重试 2 次、指数退避；400 等业务错误不重试；
- `ModelPort.stream()` 每个 delta 带**累计**的 `tool_calls`，可直接消费工具调用。

### 最小可运行示例

```python
from enter_harness import (Agent, AgentConfig, FakeModel, InMemoryStore,
                           ModelResponse, RunRequest, Scope, ToolRegistry)

tools = ToolRegistry()
tools.register("echo", lambda text="": text, "回显文本")

agent = Agent(AgentConfig(
    scope=Scope("tenant-1", "agent-1", "user-1", ("assistant",)),
    model=FakeModel([ModelResponse(content="你好，我是 enter-harness")]),
    store=InMemoryStore(),
    tools=tools,
))
result = agent.run(RunRequest(objective="调用 echo 回显 hello"))
print(result.status, result.answer)
agent.close()
```

接入模型与存储：

```python
from enter_harness.harness import build_local_ports

ports = build_local_ports("/data/agent", backend="sqlite")
# ports.memory / ports.knowledge / ports.state / ports.audit
```

把审计、状态、审批、验收接进 `Agent`：

```python
from enter_harness.harness import RuleVerifyPort, SQLiteApprovalQueue

AgentConfig(
    scope=..., model=..., store=..., tools=...,
    audit=ports.audit,
    state_port=ports.state,
    approval_queue=SQLiteApprovalQueue("/data/agent/approval.db"),
    verify_port=RuleVerifyPort(),
    verify_criteria={"require_ok": True},
)
```

不传这些字段时行为与旧版一致。

---

## 文档

- [enter-harness 使用手册](docs/使用手册-v1.md)：三套后端、PostgreSQL 完整步骤、常用流程、排错
- [宿主接入指南](docs/接入指南-v1.md)：宿主装配、调用顺序、审批恢复、常见错误
- [enter-harness 端口清单](docs/端口清单-v1.md)：10 个端口的契约与代码落点
- [enter-harness 命名规范](docs/命名规范-v1.md)：端口 / 实现 / 类型 / 字段 / 状态枚举
- [本地存储格式](docs/本地存储格式-v1.md)：SQLite / Markdown / PostgreSQL 的目录与表结构
- [核心模型：记忆 / 知识 / 上下文](docs/核心模型-记忆知识上下文-v1.md)：断言模型、召回通道、组装规则
- [enter-harness 设计](docs/enter-harness-设计.md)：模块划分、依赖方向、公开 API、迁移映射
- [运行流水线](docs/核心模型-运行流水线-v1.md)：7 个模块、10 个端口、运行闭环
- [多租户与沙箱](docs/多租户与沙箱.md)：租户隔离、权限与执行边界
- [发布流程](docs/发布.md)：版本、构建、PyPI 与 tag
- [变更记录](CHANGELOG.md)：逐版本变化

## 适用场景

适合：

- 企业级 Agent 平台：多租户、多智能体、需要审批和审计；
- 想把 Agent 运行时从业务工程里抽出来、复用到多个产品；
- 需要 PostgreSQL / 人工可读 Markdown 等多种存储形态；
- 需要把工具、记忆、知识、状态做成可替换的端口。

不适合：

- 只需要一个 prompt 脚本或一次性调用；
- 不需要治理、审计、恢复，且愿意自己维护运行时。

## 许可证

Apache License 2.0，见 [LICENSE](LICENSE)。
