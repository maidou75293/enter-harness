# Harness 使用手册 v1

> 这是 Harness（运行时治理外壳）的**唯一入口手册**：装什么、选哪套存储、怎么装配、怎么验证。
> 核对基准：2026-10-23，代码 `agent_core/harness/*`。
> 更细的端口契约见 `Harness-端口清单-v1.md`，宿主装配细节见 `Harness-接入指南-v1.md`。

## 0. 30 秒结论

Harness 的本地存储有**三套后端**，同一时刻只用一套：

```python
from agent_core.harness import build_local_ports

ports = build_local_ports("/data/agent", backend="sqlite")      # 默认
ports = build_local_ports("/data/agent", backend="markdown")    # 人可读写
ports = build_local_ports("/data/agent", backend="postgres",    # 多副本/服务端
                          dsn="postgresql://user:pass@host:5432/db")
ports.close()
```

PostgreSQL 需要可选依赖：

```bash
pip install 'agent-core[postgres]'      # 或 pip install -r requirements-postgres.txt
```

> 常见误解：`build_local_ports` 的 `root` 只对 SQLite / Markdown 生效；
> PostgreSQL 完全用 `dsn`，不会在本地建目录。

## 1. 术语

| 术语 | 中文解释 |
|---|---|
| Harness | 模型之外的运行时/治理外壳：管输入、上下文、动作、状态、验收、审计 |
| Port（端口） | 只定方法契约、不定存储与厂商的接口 |
| Backend（后端） | 端口背后的落地实现，本文指 SQLite / Markdown / PostgreSQL |
| DSN | Data Source Name，数据库连接串，形如 `postgresql://user:pass@host:5432/db` |
| R1 | 同租户、同范围、同 key、同条件的记忆只能有一条 `active` |
| WriteResult | 记忆写入结果：`append` / `supersede` / `noop` / `invalid` |

---

## 2. 三套后端对照

| 能力 | SQLite（默认） | Markdown | PostgreSQL |
|---|---|---|---|
| `MemoryPort` | `StoreMemoryPort` | `MarkdownMemoryPort` | `PostgresMemoryPort` |
| `KnowledgePort` | `SQLiteKnowledgePort` | `MarkdownKnowledgePort` | `PostgresKnowledgePort` |
| `StatePort` | `SQLiteStatePort` | SQLite（状态/审计仍落 SQLite） | `PostgresStatePort` |
| `AuditPort` | `SQLiteAuditPort` | SQLite（状态/审计仍落 SQLite） | `PostgresAuditPort` |
| 额外依赖 | 无（标准库） | 无 | `psycopg[binary]>=3.1` |
| 多进程/多副本 | 单写者，不适合 | 不适合共享目录 | 适合 |
| 人可直接编辑 | 否 | 是 | 否 |
| 适用 | 单机、开发、小数据 | 人工维护的知识/画像 | 服务端、多副本、并发写 |

选择建议：

- 先默认 SQLite；只有需要“人改文件”时才用 Markdown；
- 需要多副本、服务端部署、并发写 → PostgreSQL；
- 审计合规不要用 Markdown（哈希链与并发写不适用）。

---

## 3. PostgreSQL 完整步骤

### 3.1 安装驱动

```bash
pip install 'agent-core[postgres]'
# 等价：pip install 'psycopg[binary]>=3.1'
```

驱动是**延迟导入**的：没装 `psycopg` 也能 `import agent_core`，
只有真正创建 PostgreSQL 端口时才报错并提示安装命令。

### 3.2 准备 DSN

优先级从高到低：

1. 代码里传 `dsn=...`；
2. 环境变量 `HARNESS_PG_DSN`；
3. 环境变量 `DATABASE_URL`；
4. 标准 libpq 变量 `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` 自动拼装。

```bash
export HARNESS_PG_DSN='postgresql://harness:******@10.0.0.20:5432/harness'
```

密码含特殊字符时要 URL 编码；连接超时为 5 秒。

### 3.3 数据库权限

端口构造时会自动执行 `CREATE TABLE IF NOT EXISTS` 与 `CREATE INDEX IF NOT EXISTS`，
所以账号至少需要：

- `USAGE, CREATE ON SCHEMA public`（PG 15+ 默认收回了 public 的 CREATE）；
- 目标库的 `CONNECT`；
- 表建好后的 `SELECT / INSERT / UPDATE`；
- advisory lock 是内置功能，不需要额外授权。

最小授权示例（表由 DBA 预建时可只给 DML）：

```sql
GRANT USAGE, CREATE ON SCHEMA public TO harness;
```

### 3.4 建端口

```python
from agent_core.harness import build_local_ports

ports = build_local_ports(
    root="/data/agent",                 # PostgreSQL 下只是占位，不会建目录
    backend="postgres",
    dsn="postgresql://harness:******@10.0.0.20:5432/harness",
)

# ports.memory / ports.knowledge / ports.state / ports.audit
# 进程退出或热重载前必须关闭
ports.close()
```

也可以不传 `dsn`，让端口读 `HARNESS_PG_DSN`：

```python
ports = build_local_ports("/data/agent", backend="postgres")
```

### 3.5 自动创建的表

全部带 `harness_` 前缀，不与业务表混用：

| 表 | 用途 |
|---|---|
| `harness_memory` | 长期记忆；R1 用部分唯一索引 `WHERE status='active'` |
| `harness_knowledge_chunk` | 知识块；审核状态 + 来源 |
| `harness_run_state` | 线程最新状态 |
| `harness_run_checkpoint` | 检查点与 `parent_id` 链 |
| `harness_audit` | 审计事件与哈希链 |

`condition` / `provenance` / `state` / `extra` 使用 `JSONB`。

### 3.6 并发与一致性

- 记忆 `upsert`：先取 `pg_advisory_xact_lock`，锁键为
  `(tenant_id, scope, scope_id, agent_id, kind, key, condition)`，再查再写；
  同 key 并发不会撞唯一索引；
- 审计 `emit`：固定 advisory lock 串行整条哈希链，保证 `prev_hash` 不错序；
- 每次写入都在单事务内提交，异常回滚；
- 每个端口实例持有**一个连接 + 一把线程锁**；连接池由宿主负责，
  端口本身不池化；
- 多副本部署时，每个进程各自 `build_local_ports(...)`，由数据库锁保证跨进程一致。

### 3.7 迁移边界

- 当前**没有** SQLite ↔ Markdown ↔ PostgreSQL 自动迁移命令；
- 切后端前先导出/重放数据；
- 表结构由端口自动建，没有独立 migration 版本号；升级前先备份。

---

## 4. Markdown 后端

```python
ports = build_local_ports("/data/agent", backend="markdown")
```

目录：

```text
/data/agent/
├─ memory/<tenant_id>/<scope>/<scope_id>/<id>.md
├─ knowledge/<tenant_id>/<scope>/<scope_id>/<id>.md
├─ state.db        # 状态仍落 SQLite
└─ audit.db        # 审计仍落 SQLite
```

人工编辑规则：

- 正文改了 → 读取时重算 `content_hash`，缓存失效；
- 已审核（`approved`）的知识被改正文 → 自动退回 `pending`，不会沿用旧审核；
- `archived` 不可检索、不可再 `approve`；
- 多副本不要共享同一个 Markdown 目录；密钥、个人隐私不得写入。

---

## 5. SQLite 后端

```python
ports = build_local_ports("/data/agent", backend="sqlite")
```

```text
/data/agent/
├─ memory.db
├─ knowledge.db
├─ state.db
└─ audit.db
```

单机默认，标准库实现，无额外依赖；单写者模型，不适合多副本并发写。

---

## 6. 10 个治理端口与默认实现

| 端口 | 默认实现（含三套后端） |
|---|---|
| `IdentityPort` | `StaticIdentityPort`（本地/测试） |
| `ContextPort` | 无，需宿主实现块存储 |
| `MemoryPort` | `StoreMemoryPort` / `MarkdownMemoryPort` / `PostgresMemoryPort` |
| `KnowledgePort` | `SQLiteKnowledgePort` / `MarkdownKnowledgePort` / `PostgresKnowledgePort` |
| `AccessPolicyPort` | 无，需接产品权限系统 |
| `IngestPort` | `DefaultIngestPort` |
| `ActionPort` | `ToolActionPort`（审批队列默认 `InMemoryApprovalQueue`） |
| `StatePort` | `SQLiteStatePort` / `PostgresStatePort` |
| `VerifyPort` | `RuleVerifyPort` |
| `AuditPort` | `SQLiteAuditPort` / `PostgresAuditPort` |

`PortSet` 默认全是 `None`，不会自动补默认实例；启动时：

```python
from agent_core.harness import PortSet

ports_set = PortSet(identity=..., context=..., memory=ports.memory, ...)
ports_set.check()          # 成功返回 True，失败抛 TypeError 并列出缺失端口
```

把审计、状态、审批、验收接进 `Agent` 时，用 `AgentConfig` 的可选字段：

```python
AgentConfig(
    scope=..., model=..., store=..., tools=...,
    audit=ports.audit,
    state_port=ports.state,
    approval_queue=queue,
    verify_port=RuleVerifyPort(),
    verify_criteria={"require_ok": True},
)
```

不传这些字段时行为与旧版一致。

### 6.1 工具可见性（identity + scope）

工具注册留在宿主；Harness 只在 `ActionPort.list_tools(identity, scope)` 做可见性过滤。
工具用三个字段声明限制：

| 字段 | 空值含义 | 非空含义 |
|---|---|---|
| `scope` | 请求 scope 时默认不出现 | 等于请求的 `scope`，或命中同名 `tag` 才可见 |
| `tenants` | 所有租户可见 | 仅列表内的 `tenant_id` 可见 |
| `roles` | 所有角色可见 | 与 `identity.roles`（或 `role_ids`）有交集才可见 |

```python
registry.register("export", handler, tenants=("t1",), roles=("admin",))
registry.register("echo", handler)          # 共享工具
```

- 治理声明字段：`risk`（low/normal/high）、`side_effect`（none/read/write/external）、
  `approval`（policy/always/never）；它们是**声明**，最终判定仍由策略决定；
  `risk` 留空时由 `danger` 推导（`danger=True` → high）；
- `list_tools` 和 `call` 都会过滤；即使绕过清单直接按名字调用，身份不满足也会
  返回 `status="denied"`，handler 不会执行；
- 声明了限制但身份缺失 → **默认拒绝**，不是默认放行；
- 审批恢复（`approved_override`）跳过重判，避免恢复时丢失角色信息导致误拒；
- `ToolRegistry.register` 在装配期校验工具名、JSON Schema、字段类型，失败抛
  `ValueError`；重名默认覆盖，`replace=False` 时抛错；
- `PortSet.check()` 会调用端口的 `validate()`，工具目录有问题则启动失败；
- 工具注册/注销、handler 生命周期、热更新**不在** Harness 内，属于宿主工具目录；
- `ToolActionPort` 同时接受 `agent_core.tools.ToolRegistry`（返回 dict）与
  `agent_core.tooling.ToolRegistry`（返回 `FunctionTool`），内部统一成 dict；
  执行结果也兼容 `ToolResult`，不需要宿主自己写翻译层。

### 6.2 上下文排序（D1）

`assemble()` 的块次序由 `BlockMeta.sort_key` 决定，使用
`scope_rank(kind, scope)`，与 `select_records` 保持同一套 D1 规则：

| kind | scope 优先级 |
|---|---|
| `procedural` | platform → project → user → session |
| `semantic` | project → platform → user → session |
| `profile` | user → project → platform → session |
| `episodic` / `working` | session → project → user → platform |

同一 scope 内再按 `priority`、`id` 排序。不要再用全局 `SCOPE_ORDER` 推导拼装顺序。

---

## 7. 常用流程

### 7.1 写 / 查记忆

```python
from agent_core.context import MemoryRecord

result = ports.memory.upsert(MemoryRecord(
    tenant_id="t1", scope="project", scope_id="p1",
    kind="semantic", key="项目库地址", content="10.0.0.5:3306",
    source="user",
))
print(result.action)   # append / supersede / noop / invalid

rows = ports.memory.query({
    "tenant_id": "t1", "scope": "project", "scope_id": "p1",
    "agent_id": "a1",          # 传了才按智能体隔离；不传=该范围全部智能体
})
```

写入校验：`tenant_id`、`content`、`source` 必填；`semantic` / `procedural`
还要求 `key`；非 `platform` 范围要求 `scope_id`。

### 7.2 知识入库 / 审核 / 检索

```python
cid = ports.knowledge.add({
    "tenant_id": "t1", "scope": "project", "scope_id": "p1",
    "title": "部署规范", "content": "先灰度，再全量。",
    "source": "handbook",          # 必填
})
ports.knowledge.approve(cid, reviewer="alice")

hits = ports.knowledge.search(
    "部署", top_k=4, tenant_id="t1", scope="project", scope_id="p1")
```

- 只有 `approved` 且 `source` 非空的知识会被检索到；
- 检索是本地关键词匹配（英文词 + 中文二元组），不是向量检索；
- **多租户必须显式传 `tenant_id`**：不传会跨租户检索。

### 7.3 检查点

```python
ports.state.save("thread-1", {"step": 2})
ck = ports.state.checkpoint("thread-1", "after_tool", {"step": 3})
ports.state.list_checkpoints("thread-1")
state = ports.state.resume("thread-1")   # 最近检查点，没有则回退 load()

# 可选清理：删除 cutoff 之前的状态与检查点，返回删除行数
ports.state.prune(before=0)              # 全部清掉
ports.state.prune(retention_days=30)     # 保留最近 30 天（默认）
```

`prune` 是 SQLite / PostgreSQL 状态端口的**可选能力**，不在 10 端口协议里，
因此宿主自定义 StatePort 不受影响。

### 7.4 审计

```python
ports.audit.emit(AuditEvent(tenant_id="t1", actor="u1",
                            action="tool:deploy", target="prod",
                            decision="allow"))
ports.audit.verify_chain()   # {"ok": True, "checked": 1, "broken_at": ""}
```

### 7.5 审批

```python
first = await ports.action.call(identity, "deploy", {"env": "prod"})
if first.status == "needs_approval":
    ticket = ports.action.request_approval(identity, first)
    done = await ports.action.resume(ticket.id, "approve")
```

`resume` 是幂等的：同一审批单第二次 `resume` 返回
`ok=False, status="error"`，不会重复执行工具。

---

## 8. 验证

```bash
# 单元测试（无 PostgreSQL DSN 时自动跳过 4 项）
python -m unittest discover -s tests -p "test_*.py"

# 三套后端端到端（含 Agent、审批闭环）
python tests/e2e_harness_storage.py

# 只跑 PostgreSQL
HARNESS_PG_DSN='postgresql://...' \
  python tests/e2e_harness_storage.py --backend postgres
```

预期：

```text
E2E_OK backend=sqlite ...
E2E_OK backend=markdown ...
E2E_OK backend=postgres ...
E2E_OK approval_loop
E2E_ALL_OK sqlite,markdown,postgres
```

版本核对（宿主健康检查可直接用 `agent_core.__version__`）：

```bash
python -c "import agent_core, importlib.metadata as m; print(agent_core.__version__, m.version('agent-core'))"
# 1.1.2 1.1.2
```

两者不一致只可能是装了旧 wheel；版本号唯一来源是 `agent_core/_version.py`，
`tests/test_version.py` 会拦住漂移。

---

## 9. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: psycopg` | 没装可选依赖 | `pip install 'agent-core[postgres]'` |
| `缺少 PostgreSQL DSN` | 参数和环境变量都没给 | 传 `dsn=` 或设 `HARNESS_PG_DSN` |
| 连接超时 / refused | 地址、端口、防火墙、库未启动 | 先用 `psql "$HARNESS_PG_DSN" -c 'select 1'` 验证 |
| `permission denied for schema public` | 账号无建表权限 | `GRANT USAGE, CREATE ON SCHEMA public TO harness;` |
| 知识搜不到 | 未 `approve`、无 `source`、租户/范围不匹配 | 检查 `review_status` 与查询条件 |
| 记忆写入返回 `invalid` | 缺 tenant/content/source/key/scope_id | 看 `WriteResult.reason` |
| 审计 `verify_chain()` 断链 | 曾部分 `prune` | 清理前导出归档，或整段清理并声明新基线 |
| 重复审批执行两次 | 用了旧版本或绕过了队列 | 升级到当前版本；确认走 `ToolActionPort.resume` |
| 切到 PostgreSQL 后数据是空的 | 没有迁移 | 旧后端导出后重放；当前不自动迁移 |
| `ports.close()` 后仍报连接错误 | 端口已关闭仍被使用 | 在应用关停钩子里统一关闭，并按需重建 |

---

## 10. 文档索引

| 文档 | 内容 |
|---|---|
| `Harness-端口清单-v1.md` | 10 个端口的契约与代码落点 |
| `Harness-接入指南-v1.md` | 宿主装配、调用顺序、审批恢复、常见错误 |
| `Harness-命名规范-v1.md` | 端口 / 实现 / 字段 / 状态枚举命名 |
| `本地存储格式-v1.md` | 三套后端的目录、frontmatter、表结构 |
| `核心模型-Harness流水线-v1.md` | 流水线与分层模型 |
