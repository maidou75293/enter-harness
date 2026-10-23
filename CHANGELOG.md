# 变更记录

## 1.4.0（2026-10-23）

- 新增 Anthropic 原生 Messages API provider（`anthropic` / `claude`）：
  system 合并、`tool_use` / `tool_result` 与 `input_schema` 转换、流式聚合。
- 审批恢复支持继续同一个 run：沿用 `run_id` / `trace_id`，从挂起状态接着跑。
- 新增 `ToolCatalog`：builtin / mcp / skill / subagent / host 分层、注册期配额、来源统计。
- 新增存储迁移：`migrate_ports` / `dump_ports` / `load_ports` 与 CLI，
  覆盖记忆与知识，重复迁移幂等（不迁移审计链与运行状态）。
- 新增 `OtelEventSink`：事件转 OpenTelemetry span，可选依赖 `[otel]`。
- 新增 `enter_harness.eval`：任务级评测、超时/异常判定、离线自测 CLI。
- 命名标准化：代码/文档/发行名统一到 `enter-harness` / `enter_harness`，
  `agent_core` 兼容层保留；出口白名单目录支持新旧两套命名。
- 测试：本地 363 项、公开仓 358 项通过。

## 1.3.0（2026-10-23）

- 新增 `action_mode="port"`：工具清单与执行走 `ActionPort`（可见性/策略/审批/审计），
  默认 `legacy` 行为不变；审批挂起进入 `blocked` 并记录 `pending`。

## 1.2.0（2026-10-23）

- 工程改名为 `enter-harness`；旧 `agent_core` 包提供子模块别名兼容层。
- 新增 provider 注册表（`register_provider` / `create_model` / `build_model`）。
- 新增 `ProviderModelPort`：async provider → 同步 `ModelPort`，支持 `on_delta` 流式。
- 流式补 `usage` / `finish_reason` / 累计 `tool_calls`；连接错误与 408/429/5xx 重试。
- 版本号单一来源 `enter_harness/_version.py`。

## 1.1.2

- 修复 `__version__` 与发行元数据漂移；新增版本一致性测试。

## 1.1.1

- 修复 D1 上下文排序：`BlockMeta.sort_key` 统一使用 `scope_rank(kind, scope)`。

## 1.1.0

- 冻结 `Agent.run / stream / start` 公开 API。
