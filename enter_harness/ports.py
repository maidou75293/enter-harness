"""v1.1 冻结端口协议。

核心只依赖这些 Protocol；宿主用自己的存储/模型/策略实现它们。
所有会读写数据的方法都必须带 Scope 或显式租户参数。
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Callable, Iterator, Protocol, Sequence

from .domain import (
    AgentRecord, BudgetProfile, Decision, Event, Item, ItemDraft, ItemRef,
    LedgerRow, Message, ModelDelta, ModelResponse, RetrievalRecord, RunContext,
    RunState, Scope, SessionRecord, ToolResult, Topic, ExecutorOutcome,
)


class ModelPort(Protocol):
    def complete(self, messages: Sequence[Message], tools: Sequence[dict],
                 budget: BudgetProfile) -> ModelResponse: ...

    def stream(self, messages: Sequence[Message], tools: Sequence[dict],
               budget: BudgetProfile) -> Iterator[ModelDelta]: ...

    def count_tokens(self, messages: Sequence[Message]) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class StorageTx(Protocol):
    def put_item(self, scope: Scope, item: ItemDraft) -> str: ...

    def put_topic(self, scope: Scope, topic: Topic) -> str: ...

    def put_ledger(self, scope: Scope, row: LedgerRow) -> None: ...

    def put_deps(self, run_id: str, parent: str, child: str) -> None: ...

    def log_retrieval(self, scope: Scope, record: RetrievalRecord) -> None: ...

    # 可选：按 id 读取，供 read_item/压缩回查使用
    def get_item(self, scope: Scope, item_id: str) -> Item | None: ...


class StorePort(Protocol):
    def transaction(self) -> AbstractContextManager[StorageTx]: ...

    def search(self, scope: Scope, query: str, filters: dict,
               top_k: int) -> list[ItemRef]: ...

    def migrate(self, from_version: str, to_version: str) -> None: ...

    def schema_version(self) -> str: ...


class AgentStorePort(Protocol):
    """Agent / Session 元数据存储（与记忆 StorePort 分开，可由宿主同一实现）。"""

    def create_agent(self, agent: AgentRecord) -> AgentRecord: ...
    def get_agent(self, tenant_id: str, agent_id: str) -> AgentRecord | None: ...
    def list_agents(self, tenant_id: str) -> list[AgentRecord]: ...
    def update_agent(self, agent: AgentRecord) -> AgentRecord: ...
    def delete_agent(self, tenant_id: str, agent_id: str) -> bool: ...

    def create_session(self, session: SessionRecord) -> SessionRecord: ...
    def get_session(self, tenant_id: str, session_id: str) -> SessionRecord | None: ...
    def list_sessions(self, tenant_id: str, agent_id: str) -> list[SessionRecord]: ...
    def update_session(self, session: SessionRecord) -> SessionRecord: ...


class ToolPolicyPort(Protocol):
    """工具执行策略：allow / ask / deny。数据访问策略见 AccessPolicyPort。"""

    def check(self, ctx: RunContext, action: dict) -> Decision: ...


# 兼容旧名；新代码统一用 ToolPolicyPort
PolicyPort = ToolPolicyPort


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGen(Protocol):
    def new_id(self, prefix: str = "") -> str: ...


class Tool(Protocol):
    name: str
    description: str
    parameters: dict
    parallel_safe: bool
    danger: bool

    def execute(self, ctx: RunContext, args: dict) -> ToolResult: ...


class RunHooks(Protocol):
    """压缩/记忆/落库/统计的唯一调用点。"""

    def before_model_call(self, ctx: RunContext, state: RunState) -> None: ...

    def after_tool_batch(self, ctx: RunContext, state: RunState) -> None: ...

    def on_context_pressure(self, ctx: RunContext, state: RunState) -> None: ...

    def before_finish(self, ctx: RunContext, state: RunState) -> None: ...

    def on_cancel(self, ctx: RunContext, state: RunState) -> None: ...


class Executor(Protocol):
    def execute(self, ctx: RunContext, state: RunState) -> ExecutorOutcome: ...
