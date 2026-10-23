# Harness 流水线 v1

> 一句话：Harness 是模型之外、负责一次智能体运行的运行时。
> 论文口径（arXiv 2606.20683）把它拆成 **6 项职责**：
> observation / context / control / action / state / verification。
> 本文第 2 节是其中 **context** 一项的内部拆法：
> **从定义层开始，原始状态没有记忆，跑完一轮写回，再进入下一轮。**

## 0. 论文口径（6 项职责）

| # | 职责 | 做什么 | 主流依据 | agent-core 现状 |
|---|---|---|---|---|
| 1 | observation 观察 | 接收用户输入、工具输出、环境变化 | 2606.20683 | 部分：消息/工具结果已接，无统一入口 |
| 2 | context 上下文 | 选择、压缩、拼装、注入 | 2606.20683、CoALA 记忆轴 | 较完整：`context/` |
| 3 | control 控制 | 决策循环、计划、停止条件 | 2606.20683、CoALA 决策过程 | 有：`loop/`、`executors.py` |
| 4 | action 行动 | 工具调用、执行、沙箱 | 2606.20683、CoALA 动作空间 | 有：`tools/`、`policy.py` |
| 5 | state 状态 | 会话持久化、中断恢复 | 2606.20683、2605.18747 | 部分：`session/`、`context/store.py` |
| 6 | verification 验证 | 结果校验、失败恢复、审计 | 2606.20683、2605.18747 | 部分：`hooks/`、verifier、trace |

三条性质（arXiv 2605.18747）作为验收线：**可执行、可检查、有状态**。

## 1. 定位

| 概念 | 定义 |
|---|---|
| Harness | 运行时骨架：跑一次智能体所需的全部机制 |
| ContextManager | Harness 的一段：把多来源内容拼成模型窗口 |
| 产品 | 提供策略：记什么、什么时候记、谁能改、提示词正文 |

判断标准不变：**机制进 Harness，策略留产品。**

## 2. Context 循环（定义层 + 6 步 + 观测）

> **来源说明：这个拆法不是行业标准，是我们自己总结的内部工程拆法。**
> 行业/论文口径只到「context」这一项（见 §0 的 6 项职责）。
> 对外不要称其为行业规范。

### 2.0 总图

```text
定义层：红线/规范 · 身份 · 画像      ← 首轮就有，外部给定，不是采集来的
        │
        ▼
① 采集 ─→ ② 选择/预算 ─→ ③ 拼装 ─→ ④ 请求
  ▲                                      │
  │                                      ▼
  └──── ⑥ 归一存储 ←──── ⑤ 回流
                                          
⑦ 观测：横切 ①~⑥（只读，不改结果）
```

### 2.1 定义层 Define（a priori）

- **目标**：在任何采集之前，先确定这一轮的边界和视角。
- **内容**：

| 项 | 包括 | 性质 |
|---|---|---|
| 规范 / 红线 | 平台制度、项目规则、角色要求、SOP | 外部配置，只读，不能被子系统覆盖 |
| 身份 | 租户、用户、角色、部门、可写范围 | 账号体系提供，只读 |
| 画像 | 表达偏好、称呼、格式要求 | 用户可改的配置，可允许从记忆归纳更新 |

- **规则**：
  - 定义层不是记忆，不从记忆表采集；
  - 记忆不能覆盖红线；身份以账号系统为准，模型自述无效；
  - 画像变化要有版本或审计，且用户可见、可撤销。
- **失败处理**：身份缺失 → 拒绝；规范缺失 → 用平台默认并标记；画像缺失 → 空，不猜。
- **现状**：身份有 `IdentityPort`，规范靠 `AccessPolicyPort` + 提示词层；画像还没有独立配置实体。

### 2.2 采集 Collect（含读时归一）

- **目标**：把这一轮可能用到的东西找出来，先拿元数据，不急着拿正文。
- **来源**：会话历史、摘要、记忆、知识、工具结果、附件指针。**不含定义层**。
- **读时归一**：外部格式（JSON、Markdown、数据库行、工具输出）在这一步统一成 `Block`：
  - 枚举小写归一；未知 kind 退回 `semantic`，未知 scope 退回 `project`；
  - `content_hash` 对实际注入文本计算；非法 subtype 丢弃；
  - 每块带来源、范围、指纹。
- **冷启动**：第一轮记忆为空是正常状态，不是错误；采集结果可以只有定义层 + 知识 + 当前消息。
- **失败处理**：某个来源超时/不可用 → 记 trace，继续组装；不把整轮拖垮。
- **现状**：5 个端口已有；`DefaultIngestPort` 已提供统一入口，产品只需注入自己的来源适配器。

### 2.3 选择与预算 Select & Budget

- **目标**：留下这一轮该看的，排好序，并在有限窗口里放下。
- **选择规则**：
  - 硬过滤：租户、范围、`status=active`、过期、读写权限；
  - 跨范围同 key 只留最具体的一条，其余标记被压制；
  - 同 id / 同正文只留一条；
  - 排序：kind 次序 → scope 优先级 → priority → 新近度；
  - 鉴权异常按拒绝处理（fail closed）。
- **预算规则**：
  - 三层上限：单块 `max_chars`、分段上限、总预算（默认 32000 字）；
  - 高权威先放；放不下时整段丢弃优先于截半段；
  - 规范/指令段永不截断，超长报错或拆分；
  - 长文、工具结果只放指针，按需再取；
  - 拼装期间不改 `use_count` 等排序因子。
- **失败处理**：超预算必须写 trace，禁止静默丢；策略服务挂了就拒绝。
- **现状**：`select_records` + `pipeline.py` 已实现；已按定稿改为整块丢弃，`procedural` 超限抛 `ContextOverflow`（不截断）。

### 2.4 拼装 Compose

- **目标**：按固定次序拼成最终文本，并留下可回放的证据。
- **输出**：`text` + `blocks` + `trace` + `used_chars` + `dropped` + `truncated`。
- **规则**：
  - 固定 9 段次序：指令 → 身份 → 经验 → 语义 → 偏好 → 知识 → 摘要 → 对话 → 任务；
  - 同 id / 同正文只出现一次；知识与记忆重复以知识为准；
  - 每块记：id、kind、authority、scope、version、hash、chars、缓存状态；
  - 纯函数：不调模型、不写库、不改状态。
- **失败处理**：缺正文保留块位但不猜；指纹不一致记 `hash_mismatch`。
- **现状**：`assemble()` 已实现，缓存键 `(id, content_hash)`；27 个模型用例覆盖。

### 2.5 请求 Request

- **目标**：把拼好的上下文交给模型，并处理模型返回。
- **包含**：
  - 提示词分层拼：CORE → RUNTIME → MEMORY → SKILLS → TASK → EXTRA；
  - 消息序列：摘要 + 最近对话 + 当前问题；
  - 工具 schema：L0 目录 + 当前任务 top-N；
  - 模型差异（工具调用格式、角色名）隔离在适配层。
- **内循环**：模型要调工具时，`请求 → 工具 → 结果 → 回到采集/拼装 → 再请求`，
  直到给出最终回答或触发停止条件。外循环是一轮结束后的写回。
- **失败处理**：token 超限先压缩再重试；任何情况下不丢规范段。
- **现状**：`prompts/registry.py`、`executors.py`、`tooling.py` 已有；缺统一缓存键约定。

### 2.6 回流 Write-back

- **目标**：把这一轮的产出整理成可存储的候选，不直接落库。
- **输入**：最终回答、工具结果、本轮结论、用户显式指令、摘要、job 结果。
- **规则**：
  - 只收「值得留」的：显式指令、稳定结论、可复用做法；不逐条收工具噪声；
  - 子代理产出归父，批量产物由父显式提炼成一条；
  - 子代理不能写程序性/共享层。
- **失败处理**：不确定的进待审队列，不直接写；丢弃原因要可查。
- **现状**：`compression.py` 的 `MemoryHooks` 已有候选生成；缺统一的「值得留」判定接口。

### 2.7 归一存储 Normalize & Store

- **目标**：把回流候选变成合法记录，写入后供下一轮采集。
- **写时归一**：与读时归一分开——
  - 定 kind / scope / scope_id / key / condition；
  - 算 `content_hash`；补 `source`、`agent_id`、`session_id`；
  - 校验必填与枚举，非法直接拒绝。
- **写入四态**：`append / supersede / noop / invalid`。
- **规则**：
  - R1：同 `(tenant, scope, scope_id, agent_id, kind, key, condition)` 只有一条有效；
  - R2：只追加，旧条失效并指向新条，失效链不成环；
  - 同 key 同内容幂等，累加来源；同 key 不同内容取代；
  - 写入前过权限门；变更留审计。
- **失败处理**：冲突走取代；非法返回 `invalid` 并给原因；禁止静默覆盖。
- **现状**：`SQLiteMemoryStore.upsert/archive` 已返回四态 `WriteResult`；校验函数 `validate_record` 已实现。写权限门仍由调用方在写入前调用 `AccessPolicyPort`（内核不替产品做权限判断）。

### 2.8 观测 Observe（横切）

- **目标**：让 ①~⑥ 可查、可复现、可审计。
- **输出**：块级 trace、指标、审计日志。
- **规则**：
  - 每个块可追到来源、指纹、被丢弃/截断的原因；
  - 指标至少：命中率、预算使用率、丢弃率、缓存命中率、陈旧率、冲突率；
  - 观测只读，不能改变组装结果；
  - 企业场景日志留存 ≥6 个月，防篡改。
- **失败处理**：观测失败不影响主链路，但要自身告警。
- **现状**：`pipeline.py` 的 trace 与 `tracing/` 已有；缺指标聚合、回放工具、审计留存。

### 2.9 冷启动与闭环

第一轮（t0）：

```text
定义层有：规范、身份、画像
记忆：空
知识：可能有
→ 采集（只有知识 + 当前消息）→ 选择/预算 → 拼装 → 请求 → 回流 → 归一存储
→ 写入第一条记忆
```

第二轮（t1）：

```text
采集时多了一条上一轮写入的记忆
→ 选择时按租户/范围/key 判断它是否该进上下文
→ 循环继续
```

验收必须覆盖：

1. 记忆为空时能正常组装，不报错；
2. 上一轮写入的记录，下一轮采集可见；
3. 红线/身份不因记忆为空而缺失。

### 2.10 与旧 8 段的差异

| 旧 8 段 | 新循环 | 变化 |
|---|---|---|
| — | 定义层 | **新增**：红线/身份/画像在最前，且不是采集来的 |
| 1 采集 | ① 采集 | 读时归一并入采集 |
| 2 归一（读） | ① 采集 | 不再单列 |
| 3 选择 + 4 预算 | ② 选择/预算 | 合并为一步 |
| 5 拼装 | ③ 拼装 | 不变 |
| 6 装载 | ④ 请求 | 装载是请求的内部打包 |
| 7 回流 | ⑤ 回流 | 只产出候选，不直接落库 |
| 2 归一（写） | ⑥ 归一存储 | **移到写回之后** |
| 8 观测 | ⑦ 观测 | 改为横切，不是顺序步骤 |

## 3. Harness 其余 5 块缺口

Context 只是 6 项职责里的第 2 项。其余 5 块现状：

| # | 块 | 已有 | 还缺 | 优先级 |
|---|---|---|---|---|
| 1 | observation | 消息、工具结果已接；5 个端口 | 统一 `IngestPort`、工具结果归一、环境事件 | P1 |
| 3 | control | `loop/`：max_steps / timeout / steer / interrupt；graph checkpoint | 统一停止条件契约、审批暂停/恢复、内外循环边界 | P1 |
| 4 | action | `tools/`、`policy.py`；`harness/action.py`；已接入 Agent（审计 / 审批） | 幂等键、MCP 治理 | P1 |
| 5 | state | `session/`、SQLite 记忆、langgraph checkpoint；`harness/state.py`；已接入 Agent | 从检查点原地 replay、保留与清理 | P1 |
| 6 | verification | `hooks/`、`loop/verifier.py`、trace；`harness/verify.py` + `audit.py`；已接入 Agent | 失败恢复策略、审计留存策略 | P1 |

横切 3 块：

| 块 | 已有 | 还缺 | 优先级 |
|---|---|---|---|
| 身份与权限 | `IdentityPort`、`AccessPolicyPort` | 智能体身份、最小权限、短时凭证 | **P0** |
| 观测 | `tracing/`、块级 trace | 指标聚合、OpenTelemetry、看板 | P2 |
| 评测 | 单元测试 | 回归集、任务级评测、模型/Harness 对比 | P2 |

**计数：5 块主缺 + 3 块横切；其中 P0 4 项。**

### 3.1 先收敛，再补缺

很多能力系统里已经有，问题是**实现散、契约缺**，不是功能没有。

| 块 | 现有实现 | 收敛动作 |
|---|---|---|
| observation | `app/api.py`、`app/events.py`、工具结果 | 新增薄 `IngestPort`，不重写采集逻辑 |
| context | `agent_core/context/`、`app/context_manager.py`、`app/core_context.py` | 内核为准，app 改 adapter |
| control | `agent_core/loop/`、`executors.py`、`app/orchestrator/` | 统一入口 `Agent.run`，orchestrator 改 adapter |
| action | `agent_core/tools/`、`agent_core/policy.py`、`app/policy.py` | 合并策略实现，补审批队列 |
| state | `agent_core/session/`、`agent_core/context/store.py`、`app/checkpoints.py` | 统一 `StatePort`，后端可插 |
| verification | `agent_core/hooks/`、`loop/verifier.py`、`tracing/` | 合并为「验收」+「审计」两个出口 |

重复实现清单（enter-agent `app/` vs `agent-core`）：

```text
context      app/context_manager.py + core_context.py  vs  agent_core/context/
memory       app/memory.py                             vs  agent_core/memory/ + context/store.py
policy       app/policy.py                             vs  agent_core/policy.py
prompts      app/prompts.py + prompt_registry.py       vs  agent_core/prompts/
loop         app/orchestrator/                         vs  agent_core/loop/ + executors.py
state        app/checkpoints.py                        vs  agent_core/session/ + loop/graph.py
```

**当初真缺的 4 项，v1 进展**：

| # | 能力 | 状态 |
|---|---|---|
| 1 | 智能体身份（非人身份、最小权限、短时凭证） | **仍缺**（需产品账号系统提供） |
| 2 | 审批闭环（ask → 人工 → resume） | ✅ 契约 + 队列 + Agent 接入 |
| 3 | 审计留存 ≥6 个月与防篡改 | ✅ 哈希链 + 留存 + Agent 接入 |
| 4 | 写入契约（`validate_record` + `invalid` 态） | ✅ 已完成 |

其余都是「已有实现 → 统一端口 → 删重复」。

收敛原则：

- 一块只留一个 owner，其余变 adapter；
- 先冻结端口，再迁实现，最后删旧代码；
- 迁移前先让新实现通过同一组契约测试。

## 4. 三条治理轴（S / L / V）

循环跑得通还不够，数据要可治理：

| 轴 | 含义 | 落点 |
|---|---|---|
| Schema | 字段与规则 | 14 字段 + R1~R5 |
| Lineage | 来源与失效链 | `source`、`provenance`、`superseded_by` |
| Version | 指纹与版本 | `content_hash`、Instruction.version |

三条轴缺一条，循环就会产出「看起来对、查不清来源」的上下文。

## 5. 机制 / 策略分工

| 事项 | Harness | 产品 |
|---|---|---|
| 写入接口与校验 | ✅ | |
| 何时写、谁来写、审不审 | | ✅ |
| 拼装次序与预算执行 | ✅ | |
| 预算数值、提示词正文 | | ✅ |
| 召回通道与过滤 | ✅ | |
| 检索阈值、排序权重 | | ✅ |
| 压缩触发机制 | ✅ | |
| 压缩策略与保留要求 | | ✅ |
| trace 与指标采集 | ✅ | |
| 指标阈值与告警 | | ✅ |

## 6. 验收口径

Harness 交付时，下面每条都要能用命令复现：

1. 同一输入两次组装，结果与 trace 一致（幂等）；
2. 超预算时整段丢弃并在 trace 标记，不输出半段；
3. 规范段超长时报错，不静默截断；
4. 租户 / 范围不匹配的记录不进入上下文；
5. 写入返回 `append / supersede / noop / invalid` 四态之一；
6. 每条注入内容都能追到 `source` 与 `content_hash`；
7. 冷启动：记忆为空时能正常组装；
8. 闭环：上一轮写入的记录下一轮采集可见。
