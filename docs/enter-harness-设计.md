# enter_harness 设计

> 状态：**已实现**（2026-10-19）。本文件与代码同步；验证输出见
> `docs/enter-harness-抽取记录.md`。
>
> 工作目录：`C:\Users\Administrator\code\enter-harness`（`~/agent-arch` 的副本）。
> 原库 `~/agent-arch` 未改动。

## 1. 目标

把 `app/` 中与业务无关的智能体内核抽成独立包 `enter_harness/`。`app/` 保留业务层
（多租户、账号、计费/审批持久化、MCP 目录、Web/API、工作手册等），只依赖 `enter_harness`；
`enter_harness` 不依赖 `app`，跨层能力通过 `contracts.py` 中的协议反转注入。

判定标准：换一个产品仍可直接复用；对外只暴露稳定接口；业务策略不写死在 core。

## 2. 分层与模块图

```
┌────────────────────────── app/（业务 harness） ──────────────────────────┐
│ api auth channels config cli handbook induction mcp_client scheduler      │
│ store tenants policy(租户策略) prompts(提示词浏览)                         │
│        │ 依赖                                                             │
└────────┼─────────────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────── enter_harness/（通用内核） ───────────────────────┐
│                                                                          │
│  loop/orchestrator ── planner / graph / node_executor / verifier          │
│        │                    │                                             │
│        │              loop/react（ReAct 子图）                            │
│        ▼                    ▼                                             │
│  loop/loop.py（单线程 plan→act→observe，中断/steering）                  │
│        │                                                                  │
│  ┌─────┴────────────┬──────────────┬─────────────┬──────────────┐        │
│  │ tools/registry   │ context/     │ prompts/    │ subagent/    │        │
│  │ policy.py        │ （窗口/压缩）│ （分层拼装）│ （隔离/深度）│        │
│  └─────┬────────────┴──────────────┴─────────────┴──────────────┘        │
│        │                                                                  │
│  hooks/  tracing/  session/  skills/  memory/  llm/                       │
│        │                                                                  │
│  contracts.py（Protocol：Provider/Settings/Store/Policy/Memory/EventSink）│
│  errors.py  runtime.py  settings.py                                       │
└──────────────────────────────────────────────────────────────────────────┘
```

依赖方向只有一个：`app → enter_harness`。core 内部分层也只能向下：
`loop → tools/context/prompts → llm/session/memory/skills → contracts`。

## 3. 依赖规则（硬约束）

1. `enter_harness/` 不得出现 `from app` / `import app`。
2. core 不读环境变量、不读业务数据库结构、不写死租户/审批/计费规则。
3. core 需要的配置通过 `Settings` 协议（`get_config/get_int/get_float/get_bool`）注入；
   需要的持久化通过 `Store`/`SessionStore`/`MemoryStore` 协议注入。
4. 权限判定通过构造参数注入的 `Policy` 对象完成；core 只定义 `allow/ask/deny` 契约与
   一个通用默认实现（`enter_harness.policy.RulePolicy`），业务策略（租户信任、审批时效）留在 app。
5. 工具沙箱通过 `Sandbox` 协议注入；core 自带参数化的 bwrap/docker/microvm 实现，但
   遮罩目录、可写目录、网络策略全部由调用方传入。
6. core 只依赖 requirements 允许的库；`loop/graph.py`、`loop/react.py` 依赖 langgraph，
   顶层 `import enter_harness` 不要求实例化它们。

## 4. 公开 API（`enter_harness/__init__.py`）

公开运行入口只有 v1.1 的 `Agent`（`run / stream / start / session`）；旧异步
`AgentLoop` 与 `Orchestrator` 已降级为内部阶段，只能从 `enter_harness.loop.*` 直接导入，
不再顶层导出（避免两个入口并存，也避免旧 `RunResult` 与 `domain.RunResult` 同名）。

| 符号 | 模块 | 说明 |
|---|---|---|
| `Agent` / `AgentConfig` / `RunHandle` / `Session` | `api` / `session_api` | v1.1 同步入口与生命周期 |
| `AgentService` / `AgentRecord` / `AgentSettings` / `SessionRecord` | `agents` / `domain` | 多智能体与会话管理（模块级 CRUD） |
| `ReactExecutor` / `GraphExecutor` / `HybridExecutor` | `executors` | 包装 LangGraph react 子图 / GraphEngine |
| ~~`AgentLoop`~~ / ~~`Orchestrator`~~ | `loop.*`（内部） | 仅内部组件使用，不对外导出 |
| `Planner` / `Verifier` / `NodeExecutor` / `GraphEngine` | `loop.*` | 编排组件，可单独用 |
| `build_react_subgraph` / `react_final_text` / `react_trace` | `loop.react` | 节点内 ReAct |
| `ToolRegistry` / `ToolSpec` / `ToolContext` / `ToolResult` | `tools.registry` | 工具注册与执行 |
| `PolicyDecision` / `RulePolicy` | `policy` | allow/ask/deny 契约与默认规则 |
| `ContextManager` / `PromptRegistry` | `context.manager` / `prompts.registry` | 上下文与提示词装配 |
| `SessionManager` | `session.manager` | 会话领域封装 |
| `SkillLibrary` | `skills.library` | SKILL.md 加载与按需注入 |
| `Memory` | `memory.memory` | 长期记忆（可注入 store/provider） |
| `SubagentRunner` | `subagent.runner` | 隔离子代理 + 工具白名单 + 深度上限 |
| `HookManager` / `HookEvent` | `hooks.manager` | 生命周期钩子 |
| `Tracer` / `TraceEvent` | `tracing.tracer` | 结构化运行轨迹与回放 |
| `OpenAICompatModel` / `build_provider` / `extract_json` | `llm.*` | 模型 provider |
| `Settings` / `Provider` / `Store` / `Policy` / `MemoryProvider` / `EventSink` | `contracts` | 协议 |
| `AgentError` 及子类 | `errors` | 错误体系 |

## 5. 核心机制

### 5.1 主循环（`loop/loop.py`）
- 单线程：`plan → act → observe`，状态只在 `RunState`。
- 停止条件：模型不再调用工具 / `max_steps` / `timeout` / 外部 `interrupt()`。
- `steer(text)` 在步与步之间插入 user 消息（可重复调用），不打断正在执行的工具。
- 每步写 `Tracer`：`step_start / model / tool / step_end / final / interrupted`。

### 5.2 工具系统（`tools/registry.py`）
- `ToolSpec`：name / description / JSON Schema / danger / source / tags / 白名单。
- 执行顺序：白名单 → `before_tool` hooks → `Policy.check` → handler → `after_tool` hooks。
- handler 异常转成 `{ok: False, error}`，不中断循环（失败可恢复）。
- `ask` 决策：有 `approver` 则等待；无则返回 `needs_approval`，由上层挂起。
- 大输出由 `OutputLimiter` 落盘 + 头尾切片；路径由 `Workspace` 限制在根目录内。

### 5.3 权限模型
- `PolicyDecision(decision="allow"|"ask"|"deny", reason, approval_id, scope)`。
- core 不写死危险工具清单；`RulePolicy` 接受 allow/deny/ask 规则，app 注入自己的 Policy。
- `ToolContext` 携带 tenant/session/run/user/args，供策略使用。

### 5.4 上下文与提示词
- `PromptRegistry`：命名层（layer + priority），支持 `override()`；动态 section 回调。
- `ContextManager`：窗口双约束（条数 + 字符），压缩幂等（标记 compacted），
  `MemoryProvider.inject()` 可选注入。
- 业务提示词（人格、租户环境、工作手册）由 app 注册为 section。

### 5.5 子代理
- `SubagentRunner(task, tools_whitelist, max_depth)`：全新消息列表，不共享父上下文；
  只返回最终文本与 trace；深度用 contextvar 计数，超限抛 `SubagentDepthExceeded`。

### 5.6 Hooks / Trace / 中断
- `HookManager` 事件：`run_start/run_end/step_start/step_end/before_tool/after_tool/error/interrupt`。
- `Tracer` 记录 `seq/ts/type/data`，支持 `replay()` 与导出；`EventSink` 可接 app 的
  `store.append_run_events_batch`，保持 UI 实时消息流。
- 中断：`AgentLoop.interrupt()` 置位；下一步开始前检查并结束，产生 `interrupted` trace。

## 6. 迁移映射（旧文件 → 新位置）

| 旧（app/） | 新（enter_harness/） | app 侧保留 |
|---|---|---|
| `model_provider.py` | `llm/openai_compat.py` + `llm/profiles.py` | 薄 re-export |
| `embedding.py` | `memory/embedding.py` | 薄 re-export |
| `memory.py` | `memory/memory.py` | 薄 re-export |
| `session.py` | `session/manager.py` | 薄 re-export |
| `skills.py` | `skills/library.py` | 子类注入租户私有目录 |
| `context_manager.py` | `context/manager.py` + `prompts/registry.py` | 子类提供业务提示词片段 |
| `tools.py` | `tools/registry.py` + `tools/output.py` + `tools/workspace.py` | 子类注册业务工具 + 审计 |
| `sandbox.py` / `egress.py` | `tools/sandbox.py` / `tools/egress.py` | 参数注入（遮罩 data 目录） |
| `policy.py` | `policy.py`（契约 + RulePolicy） | 业务策略：租户/审批存储 |
| `orchestrator/planner.py` | `loop/planner.py` | app 提示词常量 |
| `orchestrator/react.py` | `loop/react.py` | — |
| `orchestrator/node_executor.py` | `loop/node_executor.py` | — |
| `orchestrator/graph_engine.py` | `loop/graph.py` | — |
| `orchestrator/verifier.py` | `loop/verifier.py` | — |
| `orchestrator/orchestrator.py` | `loop/orchestrator.py` | 审批挂起回调 |
| （新增） | `hooks/` `tracing/` `subagent/` `contracts.py` `errors.py` `runtime.py` | — |

## 7. 实现状态与保留项

### 7.1 已抽到 core 并由 app 复用

| 旧文件 | core 位置 | app 侧 |
|---|---|---|
| `app/session.py` | `session/manager.py` | 薄 re-export |
| `app/embedding.py` | `llm/embedding.py` | 薄 re-export |
| `app/memory.py` | `memory/memory.py` | 薄 re-export |
| `app/skills.py` | `skills/library.py` | 薄 re-export（目录由 app 注入） |
| `app/sandbox.py` | `tools/sandbox.py` | 薄适配：注入业务 `MASK_PATHS` |
| `app/model_provider.py` 的纯函数 | `llm/openai_compat.py`、`llm/profiles.py` | re-export + `build_provider` 薄包装 |
| （新增） | `hooks/`、`tracing/`、`runtime.py`、`settings.py` | app 直接可用 |

app 侧新增 `app/enter_harness_bridge.py`：把旧 `ToolBus` 适配为 core
`AgentLoop` 需要的 `openai_tools / is_parallel_safe / execute(ctx=)`。
依赖方向仍是 `app → enter_harness`。

### 7.2 有意保留在 app 的部分（耦合业务）

| 文件 | 保留原因 | 后续可做 |
|---|---|---|
| `app/tools.py` | 租户配额、审批/审计落库、MCP 按租户放行、检查点、Windows 工具 | 继承 core `ToolRegistry`，覆写 `_audit/_check_extra` |
| `app/context_manager.py` | 业务提示词（租户环境/手册/分层指令/知识库/记忆） | 用 core `ContextManager + PromptRegistry` 做基类 |
| `app/orchestrator/*` | 计划模式、快路径、计费/租户归属、审批续跑、运行事件落库 | core `Orchestrator` 已留 `on_approval_pending` / 提示词注入点，逐步替换 |
| `app/egress.py` | 业务出口目录常量 `~/.agent-arch-egress` | core 已有等价实现，可按部署切换 |
| `app/api.py`、`store.py`、`tenants.py` 等 | 纯业务/持久化/接口 | 不抽 |

### 7.3 内核增强（本轮）

- 工具 schema 增加 `parallel` 标记；主循环对同批只读工具并发执行（上限 8），
  有副作用工具严格串行，结果按模型给出的顺序回填。
- 工具参数不是合法 JSON 对象时**不执行**，把错误回给模型自纠。
- 工具输出老化：最近 N 条全文，更早的超长输出折叠为头+尾（只影响发送视图）。
- 同一工具+相同参数连续失败 ≥2 次 → 注入“换方法”提示。
- `RulePolicy` 顺序改为 `deny → ask → allow`（与 Claude Code 一致）。
- 会话压缩修复 `window_keep=0`；上下文组装对“已写入会话的本轮用户消息”去重。
- `runtime.bind` 改为 token 精确恢复（嵌套安全）。

## 8. 验证方式

见 `docs/enter-harness-抽取记录.md`：每条结论附真实命令与输出。验收命令：

```bash
grep -rn "from app" enter_harness/          # 无结果
python -c "import enter_harness"            # IMPORT_OK
python -m unittest discover -s tests -p "test_*.py"
curl -s http://127.0.0.1:8098/api/health
```
