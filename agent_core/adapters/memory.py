"""测试/示例用内存适配器：InMemoryStore / FakeModel / 小工具。"""
from __future__ import annotations

import copy
import hashlib
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator, Sequence

from ..domain import (
    AgentRecord, AgentSettings, BudgetProfile, Decision, Event, Item, ItemDraft,
    ItemRef, LedgerRow, Message, ModelDelta, ModelResponse, RetrievalRecord,
    Scope, SessionRecord, Topic, ToolResult, Usage,
)


class _Tx:
    def __init__(self, store: "InMemoryStore"):
        self.store = store
        self.topics = copy.deepcopy(store._topics)
        self.items = copy.deepcopy(store._items)
        self.ledger = copy.deepcopy(store._ledger)
        self.deps = set(store._deps)
        self.logs = copy.deepcopy(store._logs)

    def put_item(self, scope: Scope, item: ItemDraft) -> str:
        iid = "it_" + uuid.uuid4().hex[:12]
        item_scope = item.scope or scope
        # 归属与可见性规则统一定义在 domain（infer_owner / is_visible），
        # 宿主存储实现复用同一套，避免两套语义漂移。
        from ..domain import infer_owner
        owner_type, owner_id = infer_owner(
            item_scope, item.owner_type, item.owner_id)
        self.items[iid] = Item(
            item_id=iid, content=item.content, scope=item_scope,
            kind=item.kind, domain=item.domain, title=item.title,
            summary=item.summary, topic_id=item.topic_id,
            categories=tuple(item.categories), evidence=tuple(item.evidence),
            owner_type=owner_type, owner_id=owner_id or "")
        return iid

    def put_topic(self, scope: Scope, topic: Topic) -> str:
        tid = topic.topic_id or ("tp_" + uuid.uuid4().hex[:10])
        self.topics[tid] = Topic(
            topic_id=tid, title=topic.title, scope=topic.scope,
            domain=topic.domain, locate=topic.locate, summary=topic.summary,
            categories=tuple(topic.categories))
        return tid

    def put_ledger(self, scope: Scope, row: LedgerRow) -> None:
        self.ledger.append(row)

    def put_deps(self, run_id: str, parent: str, child: str) -> None:
        self.deps.add((run_id, parent, child))

    def log_retrieval(self, scope: Scope, record: RetrievalRecord) -> None:
        self.logs.append(record)

    def get_item(self, scope: Scope, item_id: str) -> Item | None:
        it = self.items.get(item_id)
        if it is None:
            return None
        return it if _visible(it.scope, scope) else None


class InMemoryStore:
    """事务型内存存储；只用于测试/示例。"""

    def __init__(self) -> None:
        self._topics: dict[str, Topic] = {}
        self._items: dict[str, Item] = {}
        self._ledger: list[LedgerRow] = []
        self._deps: set[tuple[str, str, str]] = set()
        self._logs: list[RetrievalRecord] = []
        self._agents: dict[tuple[str, str], AgentRecord] = {}
        self._sessions: dict[tuple[str, str], SessionRecord] = {}
        self._lock = threading.RLock()
        self._version = "1"

    # ---------------- Agent / Session ----------------
    def create_agent(self, agent: AgentRecord) -> AgentRecord:
        with self._lock:
            key = (agent.tenant_id, agent.agent_id)
            if key in self._agents:
                from ..errors import ConflictError
                raise ConflictError(f"Agent 已存在：{agent.agent_id}")
            self._agents[key] = agent
            return agent

    def get_agent(self, tenant_id: str, agent_id: str) -> AgentRecord | None:
        with self._lock:
            return self._agents.get((tenant_id, agent_id))

    def list_agents(self, tenant_id: str) -> list[AgentRecord]:
        with self._lock:
            return [a for (tid, _), a in self._agents.items() if tid == tenant_id]

    def update_agent(self, agent: AgentRecord) -> AgentRecord:
        with self._lock:
            key = (agent.tenant_id, agent.agent_id)
            if key not in self._agents:
                from ..errors import NotFoundError
                raise NotFoundError(f"Agent 不存在：{agent.agent_id}")
            self._agents[key] = agent
            return agent

    def delete_agent(self, tenant_id: str, agent_id: str) -> bool:
        with self._lock:
            return self._agents.pop((tenant_id, agent_id), None) is not None

    def create_session(self, session: SessionRecord) -> SessionRecord:
        with self._lock:
            key = (session.tenant_id, session.session_id)
            if key in self._sessions:
                from ..errors import ConflictError
                raise ConflictError(f"Session 已存在：{session.session_id}")
            self._sessions[key] = session
            return session

    def get_session(self, tenant_id: str, session_id: str) -> SessionRecord | None:
        with self._lock:
            return self._sessions.get((tenant_id, session_id))

    def list_sessions(self, tenant_id: str, agent_id: str) -> list[SessionRecord]:
        with self._lock:
            return [s for (tid, _), s in self._sessions.items()
                    if tid == tenant_id and s.agent_id == agent_id]

    def update_session(self, session: SessionRecord) -> SessionRecord:
        with self._lock:
            key = (session.tenant_id, session.session_id)
            if key not in self._sessions:
                from ..errors import NotFoundError
                raise NotFoundError(f"Session 不存在：{session.session_id}")
            self._sessions[key] = session
            return session

    def ensure_default_agent(self, tenant_id: str) -> AgentRecord:
        with self._lock:
            key = (tenant_id, "default")
            if key not in self._agents:
                import time
                self._agents[key] = AgentRecord(
                    agent_id="default", tenant_id=tenant_id,
                    name="默认智能体", role="", config=AgentSettings(),
                    status="active", created_at=time.time())
            return self._agents[key]

    @contextmanager
    def transaction(self):
        with self._lock:
            tx = _Tx(self)
            try:
                yield tx
            except BaseException:
                raise
            else:
                self._topics, self._items = tx.topics, tx.items
                self._ledger, self._deps = tx.ledger, tx.deps
                self._logs = tx.logs

    def search(self, scope: Scope, query: str, filters: dict,
               top_k: int) -> list[ItemRef]:
        q = (query or "").lower().strip()
        out: list[ItemRef] = []
        with self._lock:
            for it in self._items.values():
                if not _visible(it.scope, scope, it.owner_type, it.owner_id):
                    continue
                blob = " ".join((it.title, it.summary, it.content)).lower()
                if q and q not in blob:
                    continue
                score = 1.0 + (blob.count(q) if q else 0) * 0.1
                out.append(ItemRef(item_id=it.item_id, topic_id=it.topic_id,
                                   domain=it.domain, kind=it.kind,
                                   title=it.title or it.content[:60],
                                   summary=it.summary or it.content[:200],
                                   score=score, scope=it.scope))
        out.sort(key=lambda x: (-x.score, x.item_id))
        return out[:max(0, int(top_k))]

    def get_item(self, scope: Scope, item_id: str) -> Item | None:
        with self._lock:
            it = self._items.get(item_id)
        return it if it is not None and _visible(
            it.scope, scope, it.owner_type, it.owner_id) else None

    def schema_version(self) -> str:
        return self._version

    def migrate(self, from_version: str, to_version: str) -> None:
        if from_version != self._version or to_version != self._version:
            from ..errors import SchemaError
            raise SchemaError(f"不支持的内存 schema 迁移：{from_version} -> {to_version}")


def _visible(item_scope: Scope, request_scope: Scope,
             owner_type: str = "", owner_id: str = "") -> bool:
    # 可见性规则统一定义在 domain.is_visible，宿主存储实现复用同一套
    from ..domain import is_visible
    return is_visible(item_scope, request_scope, owner_type, owner_id)


class FakeModel:
    """脚本化模型：按顺序返回 ModelResponse；用于契约测试。"""

    def __init__(self, responses: Sequence[ModelResponse] | None = None,
                 handler: Callable | None = None):
        self._responses = list(responses or [])
        self._handler = handler
        self.calls: list[list[Message]] = []

    def complete(self, messages: Sequence[Message], tools: Sequence[dict],
                 budget: BudgetProfile) -> ModelResponse:
        self.calls.append(list(messages))
        if self._handler is not None:
            return self._handler(list(messages), list(tools), budget)
        if self._responses:
            return self._responses.pop(0)
        return ModelResponse(content="done")

    def stream(self, messages: Sequence[Message], tools: Sequence[dict],
               budget: BudgetProfile) -> Iterator[ModelDelta]:
        resp = self.complete(messages, tools, budget)
        if resp.content:
            for ch in resp.content:
                yield ModelDelta(content=ch)
        yield ModelDelta(usage=resp.usage, finish_reason=resp.finish_reason)

    def count_tokens(self, messages: Sequence[Message]) -> int:
        return max(1, sum(len(m.content) for m in messages) // 4)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            h = hashlib.sha256(str(text).encode("utf-8")).digest()
            out.append([b / 255.0 for b in h[:8]])
        return out


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class InMemoryPolicy:
    def __init__(self, decision: str = "allow", reason: str = ""):
        self.decision = decision
        self.reason = reason
        self.actions: list[dict] = []

    def check(self, ctx, action: dict) -> Decision:
        self.actions.append(action)
        return Decision(self.decision, self.reason)


class FixedClock:
    def __init__(self, now: datetime | None = None):
        self._now = now or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now


class UuidIdGen:
    def new_id(self, prefix: str = "") -> str:
        return f"{prefix}{uuid.uuid4().hex[:12]}" if prefix else uuid.uuid4().hex[:12]
