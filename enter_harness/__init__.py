"""enter_harness：可复用的智能体内核。

公开 API 只导出稳定符号；编排/图执行等依赖 langgraph 的组件采用惰性导入，
保证 `import enter_harness` 在只有基础依赖时也能成功。
"""
from __future__ import annotations

from ._version import __version__

# 契约与错误
from .contracts import (Approver, EventSink, MemoryProvider, Policy, PolicyDecision,
                        Provider, Settings, Store, ToolContext, ToolSpec)
from .errors import (AgentError, BlockedDependency, BudgetExceeded,
                     CancelledError, ConfigError, ConflictError, ContextOverflow,
                     Interrupted, MaxStepsExceeded, ModelError, NotFoundError,
                     PermissionDenied, ProviderError, RunTimeout,
                     SandboxUnavailable, SchemaError, ScopeError, StoreError,
                     SubagentDepthExceeded, ToolApprovalRequired, ToolError,
                     ToolNotAllowed, ToolNotFound, ToolTimeout)

# 基础设施
from .hooks import HookDenied, HookManager
from .policy import RulePolicy
from .prompts import PromptRegistry
from .runtime import bind, current_depth, current_run, current_session, current_tenant
from .settings import DictSettings, as_settings
from .tracing import Tracer

# LLM / 记忆 / 会话 / 技能
from .llm import (AnthropicCompatModel, OpenAICompatModel, ProviderModelPort,
                  build_model, build_provider, create_model, create_provider,
                  extract_json, get_profile, list_providers, register_provider,
                  tool_result, unregister_provider)
from .memory import Memory
from .session import SessionManager
from .skills import SkillLibrary

# 工具 / 上下文 / 主循环 / 子代理
# 旧异步 AgentLoop 已降级为内部模块（SubagentRunner 与旧单测从 enter_harness.loop 导入），
# 公开运行入口只有 v1.1 的 Agent；这里不再导出旧 loop 的 RunResult，避免与 domain.RunResult 同名。
from .context import (AccessPolicyPort, Block, BlockCache, BlockMeta,
                      ContextManager, ContextMetrics, ContextPort, IdentityPort,
                      KnowledgePort, MemoryPort, MemoryRecord, MetricsRegistry,
                      SQLiteMemoryStore, assemble, derive_authority,
                      select_records, summarize)
from .observability import OtelEventSink
from .subagent import SubagentRunner
from .tools import OutputLimiter, ToolCatalog, land_output, safe_path

# 内核端口（7 模块 / 10 端口）
from .harness import (ActionResult, ApprovalTicket, AuditEvent, AuditPort,
                      AuditSink, ActionPort, Checkpoint, DefaultIngestPort,
                      InMemoryApprovalQueue, IngestPort, LocalPorts,
                      MarkdownKnowledgePort, MarkdownMemoryPort,
                      MemoryContextPort, PortSet, PostgresAuditPort,
                      PostgresKnowledgePort, PostgresMemoryPort,
                      PostgresStatePort, RawInput, RuleVerifyPort,
                      SQLiteApprovalQueue, SQLiteAuditPort, SQLiteAuditSink,
                      SQLiteKnowledgePort, SQLiteStatePort, StatePort,
                      dump_ports, load_ports, migrate_ports,
                      StaticIdentityPort, StoreMemoryPort, ToolActionPort,
                      Verdict, VerifyPort, build_local_ports, write_record)

# v1.1 冻结 API（同步库）
from .agents import DEFAULT_AGENT_ID, AgentService
from .api import Agent, AgentConfig, RunHandle
from .compression import Compressor, MemoryHooks
from .domain import (AgentRecord, AgentSettings, Artifact, BudgetProfile,
                     BudgetTier, CancelToken, CompressionResult, Decision,
                     ErrorInfo, Event, Evidence, ExecutorOutcome, GraphNode,
                     GraphSpec, Item, ItemDraft, ItemRef, LedgerRow, Message,
                     ModelDelta, ModelResponse, PendingState, RetrievalRecord,
                     RunContext, RunRequest, RunResult, RunState, Scope,
                     SessionRecord, ToolCall, ToolResult, Topic, Usage,
                     infer_owner, is_visible)
from .executors import GraphExecutor, HybridExecutor, ReactExecutor
from .ports import (AgentStorePort, Clock, EventSink, Executor, IdGen,
                    ModelPort, PolicyPort, RunHooks, StorageTx, StorePort, Tool,
                    ToolPolicyPort)
from .session_api import Session
from .tooling import ToolRegistry
from .adapters.memory import (FakeModel, FixedClock, InMemoryEventSink,
                              InMemoryPolicy, InMemoryStore, UuidIdGen)

__all__ = [
    "__version__",
    # contracts / errors
    "Provider", "Settings", "Store", "Policy", "PolicyDecision", "Approver",
    "MemoryProvider", "EventSink", "ToolSpec", "ToolContext",
    "AgentError", "ConfigError", "ProviderError", "ToolError", "ToolNotFound",
    "ToolNotAllowed", "ToolApprovalRequired", "ToolTimeout", "ContextOverflow",
    "MaxStepsExceeded", "RunTimeout", "Interrupted", "SubagentDepthExceeded",
    "SandboxUnavailable",
    # infra
    "HookManager", "HookDenied", "Tracer", "RulePolicy", "PromptRegistry",
    "DictSettings", "as_settings", "bind", "current_tenant", "current_session",
    "current_run", "current_depth",
    # llm / memory / session / skills
    "OpenAICompatModel", "AnthropicCompatModel", "ProviderModelPort",
    "build_model", "build_provider",
    "create_model", "create_provider", "register_provider", "unregister_provider",
    "list_providers", "get_profile", "extract_json",
    "tool_result", "Memory", "SessionManager", "SkillLibrary",
    # tools / context / loop / subagent
    "ToolRegistry", "ToolCatalog", "OutputLimiter", "land_output", "safe_path",
    "OtelEventSink",
    "ContextManager", "SubagentRunner",
    # 统一记忆/上下文模型（v1.3）
    "assemble", "BlockCache", "Block", "BlockMeta", "MemoryRecord",
    "ContextMetrics", "MetricsRegistry", "summarize",
    "ContextPort", "IdentityPort", "MemoryPort", "KnowledgePort",
    "AccessPolicyPort", "SQLiteMemoryStore", "derive_authority", "select_records",
    # 内核端口与默认实现
    "IngestPort", "ActionPort", "StatePort", "VerifyPort", "AuditPort",
    "AuditSink",
    "PortSet", "RawInput", "ActionResult", "ApprovalTicket", "Verdict",
    "AuditEvent", "Checkpoint", "DefaultIngestPort", "StoreMemoryPort",
    "MemoryContextPort", "SQLiteKnowledgePort", "MarkdownMemoryPort",
    "MarkdownKnowledgePort", "PostgresMemoryPort", "PostgresKnowledgePort",
    "PostgresStatePort", "PostgresAuditPort", "LocalPorts",
    "build_local_ports", "StaticIdentityPort", "ToolActionPort",
    "migrate_ports", "dump_ports", "load_ports",
    "InMemoryApprovalQueue", "SQLiteApprovalQueue", "SQLiteStatePort",
    "SQLiteAuditPort", "SQLiteAuditSink", "RuleVerifyPort", "write_record",
    # v1.1 frozen API
    "Agent", "AgentConfig", "RunHandle", "Session", "Scope", "RunContext",
    "AgentService", "DEFAULT_AGENT_ID", "AgentRecord", "AgentSettings",
    "SessionRecord", "AgentStorePort", "NotFoundError",
    "infer_owner", "is_visible",
    "RunState", "RunRequest", "RunResult", "ExecutorOutcome", "Event",
    "BudgetProfile", "BudgetTier", "Usage", "Message", "ToolCall",
    "ToolResult", "ModelResponse", "ModelDelta", "Decision", "PendingState",
    "CancelToken", "CompressionResult", "Item", "ItemDraft", "ItemRef",
    "Topic", "LedgerRow", "RetrievalRecord", "Artifact", "Evidence",
    "ErrorInfo", "GraphNode", "GraphSpec", "Compressor", "MemoryHooks",
    "ReactExecutor", "GraphExecutor", "HybridExecutor", "Tool",
    "ModelPort", "StorePort", "StorageTx", "PolicyPort", "ToolPolicyPort",
    "EventSink",
    "Clock", "IdGen", "RunHooks", "Executor",
    "ScopeError", "BudgetExceeded", "PermissionDenied", "BlockedDependency",
    "ModelError", "StoreError", "ConflictError", "CancelledError", "SchemaError",
    "FakeModel", "InMemoryStore", "InMemoryEventSink", "InMemoryPolicy",
    "FixedClock", "UuidIdGen",
]

# 惰性导出（依赖 langgraph）
# Orchestrator 已降级为内部阶段：公开入口只有 Agent.run / Agent.stream / Agent.start，
# 图执行由 GraphExecutor 包装 GraphEngine + Planner/Verifier 完成，不再导出 Orchestrator。
_LAZY = {
    "Planner": (".loop.planner", "Planner"),
    "Verifier": (".loop.verifier", "Verifier"),
    "NodeExecutor": (".loop.node_executor", "NodeExecutor"),
    "GraphEngine": (".loop.graph", "GraphEngine"),
    "build_react_subgraph": (".loop.react", "build_react_subgraph"),
    "react_final_text": (".loop.react", "react_final_text"),
    "react_has_final": (".loop.react", "react_has_final"),
    "react_trace": (".loop.react", "react_trace"),
    "tool_specs": (".loop.react", "tool_specs"),
}

__all__ += list(_LAZY)


def __getattr__(name: str):
    if name in _LAZY:
        import importlib
        mod_name, attr = _LAZY[name]
        mod = importlib.import_module(mod_name, __name__)
        val = getattr(mod, attr)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
