# agent-core

> 可复用、可治理的智能体内核（agent runtime kernel）。
> 把「模型能做什么」变成「系统可控地做什么」。

`agent-core` 是一个 Python 库，不是 HTTP 服务。宿主工程通过 `pip install` 引入，
直接构造 `Agent` 运行任务；模型、存储、权限、事件、工具全部由宿主以端口（Port，
只定契约、不定厂商的接口）注入。业务代码留在宿主，内核只负责运行时治理。

![agent-core 架构图](docs/architecture.svg)

> 一张图看懂：宿主只实现业务；身份、上下文、工具、状态、审计由内核统一治理。

```python
from agent_core import Agent, AgentConfig, RunRequest, Scope

agent = Agent(AgentConfig(
    scope=Scope("tenant-1", "agent-1", "user-1", ("assistant",)),
    model=my_model,            # 实现 ModelPort 的适配器
    store=my_store,            # 实现 StorePort 的适配器
    tools=my_tools,            # ToolRegistry
))
result = agent.run(RunRequest(objective="分析本周销售数据"))
```

---

## 为什么需要它

把模型直接接到业务系统上，通常会遇到同一组问题：

- 工具谁来管？谁能看见、能不能调、调用要不要审批？
- 上下文超长怎么办？哪些能截断、哪些绝对不能？
- 任务跑到一半失败，能不能从检查点恢复？
- 多租户、多智能体之间怎么隔离？
- 出了事怎么审计？谁在什么时候对什么做了什么？
- 换个产品，这套运行时能不能直接复用？

`agent-core` 把这些问题收进内核，用统一端口和默认实现解决；
产品只实现自己的模型、存储和权限，不再各自重写一遍运行时。

---

## 核心能力

### 1. 内核与业务分离，换产品可复用

- 公开入口只有 `Agent.run / stream / start`（会话用 `Agent.session(...)`）；
- 模型、存储、权限、事件、工具全部端口化，不绑定厂商；
- `agent_core/` 不反向依赖任何业务模块，可直接被多个宿主复用。

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
from agent_core.harness import build_local_ports

ports = build_local_ports("/data/agent", backend="sqlite")      # 默认
ports = build_local_ports("/data/agent", backend="markdown")    # 人可读写
ports = build_local_ports("/data/agent", backend="postgres",    # 多副本
                          dsn="postgresql://user:pass@host:5432/db")
```

- PostgreSQL 可选依赖：`pip install 'agent-core[postgres]'`；
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
- 工具调用、审批、验收、运行开始 / 结束全部可写审计。

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
- `StorePort` / `AgentStorePort`：记忆、事务、ledger、会话；
- `ToolPolicyPort`：工具调用前 `allow / ask / deny`；
- `EventSink`：start / model / node / done 事件；
- 宿主可以只实现自己关心的端口，其余用默认实现。

### 10. 可验证

- 273 项单元测试（公开仓基线；本地含旧宿主测试时为 278）；
- 存储端到端覆盖 SQLite / Markdown / PostgreSQL，并串起
  记忆 → 上下文 → 知识审核 → 检查点 → 审计链 → Agent 运行 → 审批闭环；
- PostgreSQL 用例在未设 `HARNESS_PG_DSN` 时自动跳过，不拖累本地开发。

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
pip install "agent-core[postgres] @ git+https://github.com/maidou75293/agent-core.git@v1.1.0"

# 或本地开发
pip install -e /path/to/agent-core
```

最小用法：

```python
from agent_core import (Agent, AgentConfig, FakeModel, InMemoryStore,
                        ModelResponse, RunRequest, Scope, ToolRegistry)

tools = ToolRegistry()
tools.register("echo", lambda text="": text, "回显文本")

agent = Agent(AgentConfig(
    scope=Scope("tenant-1", "agent-1", "user-1", ("assistant",)),
    model=FakeModel([ModelResponse(content="你好，我是 agent-core")]),
    store=InMemoryStore(),
    tools=tools,
))
result = agent.run(RunRequest(objective="调用 echo 回显 hello"))
print(result.status, result.answer)
agent.close()
```

接 Harness 存储：

```python
from agent_core.harness import build_local_ports

ports = build_local_ports("/data/agent", backend="sqlite")
# ports.memory / ports.knowledge / ports.state / ports.audit
```

把审计、状态、审批、验收接进 `Agent`：

```python
from agent_core.harness import RuleVerifyPort, SQLiteApprovalQueue

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

- [Harness 使用手册](docs/Harness-使用手册-v1.md)：三套后端、PostgreSQL 完整步骤、常用流程、排错
- [Harness 接入指南](docs/Harness-接入指南-v1.md)：宿主装配、调用顺序、审批恢复、常见错误
- [Harness 端口清单](docs/Harness-端口清单-v1.md)：10 个端口的契约与代码落点
- [Harness 命名规范](docs/Harness-命名规范-v1.md)：端口 / 实现 / 类型 / 字段 / 状态枚举
- [本地存储格式](docs/本地存储格式-v1.md)：SQLite / Markdown / PostgreSQL 的目录与表结构
- [agent-core 设计](docs/agent-core-设计.md)：模块划分、依赖方向、公开 API、迁移映射
- [Harness 流水线](docs/核心模型-Harness流水线-v1.md)：7 个模块、10 个端口、运行闭环
- [多租户与沙箱](docs/多租户与沙箱.md)：租户隔离、权限与执行边界

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
