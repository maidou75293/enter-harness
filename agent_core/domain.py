"""冻结接口的数据模型（v1.1）。

这里只放值对象/状态对象，不放业务逻辑；所有端口和公开 API 都围绕这些类型。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from .errors import CancelledError


# ---------------------------------------------------------------- 作用域
@dataclass(frozen=True, init=False)
class Scope:
    """租户 / 智能体 / 用户 / 角色组合；Agent 按 Scope 一实例。

    新签名为 ``(tenant_id, agent_id, user_id, role_ids)``；为了兼容旧调用，
    ``Scope("t1", "u1", ["arch"])`` 仍按旧顺序 ``(tenant, user, roles)`` 解析。
    新代码建议全部用关键字，避免位置歧义。
    """

    tenant_id: str
    agent_id: str = "default"
    user_id: str = ""
    role_ids: tuple[str, ...] = ()

    def __init__(self, tenant_id: str, *args,
                 agent_id: str | None = None, user_id: str | None = None,
                 role_ids: tuple[str, ...] | list[str] | None = None):
        a = u = r = None
        if len(args) == 1:
            # 旧接口 Scope(tenant, user)
            u = args[0]
        elif len(args) == 2:
            if isinstance(args[1], (list, tuple)) and not isinstance(args[1], str):
                # 旧接口 Scope(tenant, user, roles)
                u, r = args[0], args[1]
            else:
                # 新接口 Scope(tenant, agent, user)
                a, u = args[0], args[1]
        elif len(args) == 3:
            a, u, r = args[0], args[1], args[2]
        elif len(args) > 3:
            raise TypeError("Scope 参数过多")
        if agent_id is not None:
            a = agent_id
        if user_id is not None:
            u = user_id
        if role_ids is not None:
            r = role_ids
        tid = str(tenant_id or "").strip()
        if not tid:
            raise ValueError("Scope.tenant_id 不能为空")
        aid = str(a or "default").strip() or "default"
        roles = tuple(sorted({str(x).strip() for x in (r or ()) if str(x).strip()}))
        object.__setattr__(self, "tenant_id", tid)
        object.__setattr__(self, "agent_id", aid)
        object.__setattr__(self, "user_id", str(u or "").strip())
        object.__setattr__(self, "role_ids", roles)


def infer_owner(scope: Scope, owner_type: str = "",
                owner_id: str = "") -> tuple[str, str]:
    """未显式声明归属时的推断规则（宿主存储实现复用）。

    非 default 智能体 → agent（跨用户共享、跨智能体隔离）；
    其次 user → role → tenant。
    """
    if owner_type:
        return owner_type, owner_id or ""
    if scope.agent_id and scope.agent_id != "default":
        return "agent", scope.agent_id
    if scope.user_id:
        return "user", scope.user_id
    if scope.role_ids:
        return "role", scope.role_ids[0]
    return "tenant", scope.tenant_id


def is_visible(item_scope: Scope, request_scope: Scope,
               owner_type: str = "", owner_id: str = "") -> bool:
    """记忆可见性（宿主存储实现也应复用这一规则）。

    - platform：跨租户可见；
    - agent：同一智能体共享（跨用户/角色），不同智能体隔离；
    - user：所有者命中（item 上的 role_ids 作附加约束）；
    - role：角色命中（item 上的 user_id 作附加约束）；
    - tenant：同租户可见；
    - 未声明 owner_type 的旧数据：按 Scope 字段兼容。
    """
    if item_scope.tenant_id == "platform" or owner_type == "platform":
        return True
    if item_scope.tenant_id != request_scope.tenant_id:
        return False
    roles = set(request_scope.role_ids)
    if owner_type == "agent":
        return bool(owner_id) and owner_id == request_scope.agent_id
    if owner_type == "user":
        return ((bool(owner_id) and owner_id == request_scope.user_id)
                and (not item_scope.role_ids or bool(set(item_scope.role_ids) & roles)))
    if owner_type == "role":
        return ((bool(owner_id) and owner_id in roles)
                and (not item_scope.user_id
                     or item_scope.user_id == request_scope.user_id))
    if owner_type == "tenant":
        return True
    if item_scope.user_id and item_scope.user_id != request_scope.user_id:
        return False
    if item_scope.role_ids and not set(item_scope.role_ids) & roles:
        return False
    if (item_scope.agent_id and item_scope.agent_id != "default"
            and item_scope.agent_id != request_scope.agent_id):
        return False
    return True


@dataclass(frozen=True)
class AgentSettings:
    """Agent 级配置：模型档位 / 默认执行器 / 工具白名单 / 提示词档。"""

    model_tier: str = "medium"
    executor: str = "auto"
    tool_whitelist: tuple[str, ...] = ()
    prompt_profile: str = "default"

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_tier", str(self.model_tier or "medium"))
        object.__setattr__(self, "executor", str(self.executor or "auto"))
        object.__setattr__(self, "prompt_profile",
                           str(self.prompt_profile or "default"))
        object.__setattr__(self, "tool_whitelist", tuple(
            str(t).strip() for t in (self.tool_whitelist or ()) if str(t).strip()))


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    tenant_id: str
    name: str
    role: str = ""
    config: AgentSettings = field(default_factory=AgentSettings)
    status: str = "active"
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    tenant_id: str
    agent_id: str
    user_id: str = ""
    title: str = ""
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------- 消息/模型
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""
    name: str = ""
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {
                     "name": c.name,
                     # OpenAI 兼容接口要求 arguments 是 JSON 字符串
                     "arguments": (c.arguments if isinstance(c.arguments, str)
                                   else json.dumps(c.arguments, ensure_ascii=False))}}
                for c in self.tool_calls
            ]
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.name:
            out["name"] = self.name
        return out


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0

    def merge(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost=self.cost + other.cost,
        )


@dataclass(frozen=True)
class ModelResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"


@dataclass(frozen=True)
class ModelDelta:
    content: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    ok: bool
    content: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)

    def as_message(self) -> Message:
        return Message(role="tool", content=self.content if self.ok else f"错误：{self.error}",
                       tool_call_id=self.call_id, name=self.name)


# ---------------------------------------------------------------- 预算
@dataclass(frozen=True)
class BudgetProfile:
    max_steps: int = 8
    max_tokens: int = 30000
    max_seconds: float = 300.0
    max_cost: float = 0.0


class BudgetTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------- 记忆/知识
@dataclass(frozen=True)
class ItemRef:
    item_id: str
    topic_id: str = ""
    domain: str = "memory"
    kind: str = "fact"
    title: str = ""
    summary: str = ""
    score: float = 0.0
    scope: Scope | None = None


@dataclass(frozen=True)
class Evidence:
    type: str
    ref: str


@dataclass(frozen=True)
class ItemDraft:
    content: str
    kind: str = "fact"
    title: str = ""
    summary: str = ""
    domain: str = "memory"
    topic_id: str = ""
    categories: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    confidence: float = 0.6
    fingerprint: str = ""
    scope: Scope | None = None
    owner_type: str = ""       # platform | tenant | user | role | agent
    owner_id: str = ""


@dataclass(frozen=True)
class Item:
    item_id: str
    content: str
    scope: Scope
    kind: str = "fact"
    domain: str = "memory"
    title: str = ""
    summary: str = ""
    topic_id: str = ""
    categories: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    status: str = "active"
    version: int = 1
    owner_type: str = ""
    owner_id: str = ""


@dataclass(frozen=True)
class Topic:
    topic_id: str
    title: str
    scope: Scope
    domain: str = "memory"
    locate: str = ""
    summary: str = ""
    categories: tuple[str, ...] = ()
    owner_type: str = ""
    owner_id: str = ""


@dataclass(frozen=True)
class LedgerRow:
    run_id: str
    node_id: str
    objective: str = ""
    status: str = "ok"
    digest: str = ""
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    tokens_used: int = 0
    last_referenced_at: float = field(default_factory=time.time)
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RetrievalRecord:
    query_id: str
    scope: Scope
    query_text: str
    domain: str = ""
    categories: tuple[str, ...] = ()
    returned_ids: tuple[str, ...] = ()
    adopted_ids: tuple[str, ...] = ()
    rejected_citations: tuple[str, ...] = ()
    ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------- 运行态
@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    retryable: bool = False
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PendingState:
    kind: str
    prompt: str = ""
    data: dict = field(default_factory=dict)
    resume_token: str = ""


@dataclass(frozen=True)
class Artifact:
    path: str
    sha256: str = ""
    kind: str = "file"


@dataclass(frozen=True)
class CancelToken:
    """线程安全的取消标记；执行器在模型/工具边界检查。"""

    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def is_cancelled(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError("运行已取消")


@dataclass(frozen=True)
class Decision:
    decision: Literal["allow", "ask", "deny"] = "allow"
    reason: str = ""
    approval_id: str = ""


# ---------------------------------------------------------------- 请求/结果
@dataclass(frozen=True)
class GraphNode:
    node_id: str
    type: Literal["tool", "llm", "react"] = "llm"
    tool: str = ""
    args: dict = field(default_factory=dict)
    prompt: str = ""
    depends_on: tuple[str, ...] = ()
    max_steps: int = 6
    retry: int = 0


@dataclass(frozen=True)
class GraphSpec:
    nodes: tuple[GraphNode, ...] = ()
    final_node: str = ""


@dataclass(frozen=True)
class RunRequest:
    objective: str
    inputs: tuple[Any, ...] = ()
    acceptance: tuple[str, ...] = ()
    executor: str = ""
    budget: BudgetProfile | None = None
    metadata: dict = field(default_factory=dict)
    idempotency_key: str = ""
    graph: GraphSpec | None = None
    cancel_token: CancelToken | None = None
    parent_run_id: str = ""


@dataclass(frozen=True)
class RunContext:
    run_id: str
    parent_run_id: str | None
    trace_id: str
    idempotency_key: str | None
    scope: Scope
    objective: str
    acceptance: list[str]
    budget: BudgetProfile
    tools: Any
    tool_whitelist: list[str]
    executor: str
    metadata: dict


@dataclass
class RunState:
    messages: list[Message] = field(default_factory=list)
    step: int = 0
    tool_results: list[ToolResult] = field(default_factory=list)
    retrieved: list[ItemRef] = field(default_factory=list)
    summary: str | None = None
    ledger: list[LedgerRow] = field(default_factory=list)
    memory_delta: list[ItemDraft] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    status: str = "running"
    result: Any | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    error: ErrorInfo | None = None
    pending: PendingState | None = None
    cancel_token: CancelToken = field(default_factory=CancelToken)


@dataclass(frozen=True)
class ExecutorOutcome:
    status: str
    answer: Any | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: ErrorInfo | None = None
    pending: PendingState | None = None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    trace_id: str
    status: str
    answer: Any | None
    artifacts: list[Artifact] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    error: ErrorInfo | None = None
    pending: PendingState | None = None
    state: RunState | None = None


@dataclass(frozen=True)
class CompressionResult:
    compressed: bool
    summary: str = ""
    removed_messages: int = 0
    kept_messages: int = 0
    over_limit: bool = False


# ---------------------------------------------------------------- 事件
@dataclass(frozen=True)
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    run_id: str = ""
    trace_id: str = ""
    ts: float = field(default_factory=time.time)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
