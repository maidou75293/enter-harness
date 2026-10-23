"""冻结模块 API（v1.1）：Agent / AgentConfig / Session / RunHandle。

设计要点：
- Agent 按 Scope 一实例，线程安全、可复用；
- Session 非线程安全，每会话串行；
- 执行器只读写 RunState，RunContext 只读；
- 端口全部由宿主注入，核心不读环境变量/全局状态。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from .domain import (
    BudgetProfile, CancelToken, ErrorInfo, Event, ExecutorOutcome, PendingState,
    RunContext, RunRequest, RunResult, RunState, Scope, Usage,
)
from .errors import ConfigError, ScopeError
from .ports import (Clock, EventSink, IdGen, ModelPort, RunHooks, StorePort,
                    ToolPolicyPort)
from .tooling import ToolRegistry


class _SystemClock:
    def now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)


class _UuidIds:
    def new_id(self, prefix: str = "") -> str:
        return (prefix or "") + uuid.uuid4().hex[:12]


class _NullEvents:
    def emit(self, event: Event) -> None:
        return None


class _AllowPolicy:
    def check(self, ctx, action):
        from .domain import Decision
        return Decision("allow", "默认允许")


@dataclass
class AgentConfig:
    scope: Scope
    model: ModelPort
    store: StorePort
    policy: ToolPolicyPort | None = None
    events: EventSink | None = None
    clock: Clock | None = None
    ids: IdGen | None = None
    tools: ToolRegistry | None = None
    tool_whitelist: list[str] = field(default_factory=list)
    executor: str = "auto"          # auto | react | graph | hybrid
    budget: BudgetProfile = field(default_factory=BudgetProfile)
    hooks: RunHooks | None = None
    metadata: dict = field(default_factory=dict)
    retrieval_top_k: int = 5
    # 内核运行时端口（默认 None = 保持旧行为）
    audit: Any | None = None
    state_port: Any | None = None
    approval_queue: Any | None = None
    verify_port: Any | None = None
    verify_criteria: dict | None = None


class Agent:
    """一个 Scope 一个实例；线程安全、可复用。"""

    def __init__(self, config: AgentConfig):
        if not isinstance(config.scope, Scope):
            raise ScopeError("AgentConfig.scope 必须是 Scope")
        if config.model is None:
            raise ConfigError("缺少 model 端口")
        if config.store is None:
            raise ConfigError("缺少 store 端口")
        if config.executor not in ("auto", "react", "graph", "hybrid"):
            raise ConfigError(f"未知 executor：{config.executor}")
        self.config = config
        self.scope = config.scope
        self.model = config.model
        self.store = config.store
        self.policy = config.policy or _AllowPolicy()
        self.events = config.events or _NullEvents()
        self.clock = config.clock or _SystemClock()
        self.ids = config.ids or _UuidIds()
        self.tools = config.tools or ToolRegistry()
        from .compression import Compressor, MemoryHooks
        self.compressor = Compressor(self.model)
        self.hooks = config.hooks or MemoryHooks(self.compressor)
        self._closed = False
        self._idempotent: dict[str, RunResult] = {}
        self._lock = threading.RLock()

    # ---------------- 生命周期 ----------------
    def close(self) -> None:
        with self._lock:
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ---------------- 内部：上下文/状态 ----------------
    def _resolve_executor(self, req: RunRequest) -> str:
        explicit = str(req.executor or self.config.executor or "auto").strip().lower()
        if explicit not in ("auto", "react", "graph", "hybrid"):
            raise ConfigError(f"未知 executor：{explicit}")
        if explicit != "auto":
            return explicit
        graph = req.graph
        if graph is not None and graph.nodes:
            if any(n.type == "react" for n in graph.nodes):
                return "hybrid"
            return "graph"
        return "react"

    def _build_context(self, req: RunRequest) -> RunContext:
        if not str(req.objective or "").strip():
            raise ConfigError("RunRequest.objective 不能为空")
        run_id = self.ids.new_id("run_")
        trace_id = self.ids.new_id("trace_")
        budget = req.budget or self.config.budget
        meta = {**self.config.metadata, **dict(req.metadata or {})}
        meta.setdefault("audit", self.config.audit)
        meta.setdefault("state_port", self.config.state_port)
        meta.setdefault("approval_queue", self.config.approval_queue)
        meta.setdefault("verify_port", self.config.verify_port)
        meta["graph"] = req.graph
        return RunContext(
            run_id=run_id,
            parent_run_id=req.parent_run_id or None,
            trace_id=trace_id,
            idempotency_key=req.idempotency_key or None,
            scope=self.scope,
            objective=str(req.objective),
            acceptance=list(req.acceptance or ()),
            budget=budget,
            tools=self.tools,
            tool_whitelist=list(self.config.tool_whitelist or ()),
            executor=self._resolve_executor(req),
            metadata=meta,
        )

    def _build_state(self, ctx: RunContext, req: RunRequest) -> RunState:
        messages = []
        for m in (req.metadata or {}).get("history", []) or []:
            if isinstance(m, dict):
                from .domain import Message
                messages.append(Message(role=m.get("role", "user"),
                                        content=str(m.get("content") or "")))
            else:
                messages.append(m)
        from .domain import Message
        messages.append(Message(role="user", content=str(req.objective)))
        return RunState(messages=messages,
                        cancel_token=req.cancel_token or CancelToken())

    # ---------------- 内核端口（可选） ----------------
    def _audit(self, ctx: RunContext, action: str, target: str,
               decision: str, reason: str = "") -> None:
        if self.config.audit is None:
            return
        try:
            from .harness import AuditEvent
            self.config.audit.emit(AuditEvent(
                tenant_id=str(getattr(ctx.scope, "tenant_id", "") or ""),
                actor=str(getattr(ctx.scope, "user_id", "") or ""),
                action=action, target=str(target)[:200], decision=decision,
                reason=str(reason)[:500], trace_id=ctx.trace_id))
        except Exception:  # noqa: BLE001 —— 审计失败不拖垮主链路
            pass

    def _checkpoint(self, ctx: RunContext, node: str,
                    payload: dict | None = None) -> None:
        if self.config.state_port is None:
            return
        try:
            self.config.state_port.checkpoint(ctx.run_id, node, dict(payload or {}))
        except Exception:  # noqa: BLE001 —— 检查点失败不拖垮主链路
            pass

    def _apply_resume(self, ctx: RunContext, req: RunRequest) -> None:
        """把上一轮挂起的审批结论落到队列，并留审计。"""
        decision = dict((req.metadata or {}).get("approval_decision") or {})
        if not decision:
            return
        ok = self._truthy(decision.get("decision"))
        tool = str(decision.get("tool") or "")
        approval_id = str(decision.get("approval_id") or "")
        queue = self.config.approval_queue
        if queue is not None and approval_id:
            try:
                ticket = queue.get(approval_id)
                if ticket is not None:
                    ticket.status = "approved" if ok else "rejected"
                    ticket.decided_at = time.time()
                    ticket.decided_by = str(getattr(ctx.scope, "user_id", "") or "")
                    queue.update(ticket)
            except Exception:  # noqa: BLE001
                pass
        self._audit(ctx, f"approval:{tool}", tool,
                    "approved" if ok else "denied",
                    str(decision.get("reason") or ""))

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in ("1", "true", "yes", "ok", "allow", "approved",
                        "approve", "通过", "同意")

    # ---------------- 公开调用 ----------------
    def _executor_for(self, name: str):
        from .executors import GraphExecutor, HybridExecutor, ReactExecutor
        if name == "react":
            return ReactExecutor()
        if name == "graph":
            return GraphExecutor()
        if name == "hybrid":
            return HybridExecutor()
        raise ConfigError(f"未知 executor：{name}")

    def _commit(self, ctx: RunContext, state: RunState, status: str) -> None:
        """执行器只追加 memory_delta；这里统一事务提交。"""
        if status not in ("ok", "partial"):
            return
        from .domain import RetrievalRecord
        with self.store.transaction() as tx:
            for draft in state.memory_delta:
                tx.put_item(ctx.scope, draft)
            for row in state.ledger:
                tx.put_ledger(ctx.scope, row)
            if state.retrieved:
                tx.log_retrieval(ctx.scope, RetrievalRecord(
                    query_id=self.ids.new_id("q_"), scope=ctx.scope,
                    query_text=ctx.objective,
                    returned_ids=tuple(x.item_id for x in state.retrieved)))

    def _run(self, req: RunRequest, sink=None) -> RunResult:
        if self._closed:
            raise ConfigError("Agent 已关闭")
        key = req.idempotency_key or ""
        if key:
            with self._lock:
                if key in self._idempotent:
                    return self._idempotent[key]
        from .run_hooks import NoopRunHooks
        ctx = self._build_context(req)
        state = self._build_state(ctx, req)
        hooks = self.hooks or NoopRunHooks()
        event_sink = sink or self.events
        ctx.metadata.update({"model": self.model, "policy": self.policy,
                             "hooks": hooks, "event_sink": event_sink})
        self._apply_resume(ctx, req)
        self._audit(ctx, "run:start", ctx.objective, "start")
        self._checkpoint(ctx, "start", {
            "objective": ctx.objective, "executor": ctx.executor,
            "messages": [m.as_dict() for m in state.messages],
        })
        event_sink.emit(Event("start", {"executor": ctx.executor}, ctx.run_id, ctx.trace_id))
        # L3 检索：只填充 RunState.retrieved，是否注入由执行器/宿主决定
        try:
            state.retrieved = self.store.search(
                ctx.scope, ctx.objective, {},
                int(self.config.retrieval_top_k or 5))
        except Exception:
            state.retrieved = []
        executor = self._executor_for(ctx.executor)
        try:
            outcome = executor.execute(ctx, state)
            # 冻结接口兜底：react 预算耗尽且提供了图模板 → 升级 hybrid
            if (outcome.status == "failed" and outcome.error is not None
                    and outcome.error.code == "budget_exceeded"
                    and ctx.executor == "react" and req.graph is not None):
                from dataclasses import replace as _replace
                from .executors import HybridExecutor
                hctx = _replace(ctx, executor="hybrid")
                outcome = HybridExecutor().execute(hctx, state)
        except Exception as e:  # noqa: BLE001 —— 统一收敛为 RunResult.error
            from .errors import AgentError as _AgentError, CancelledError as _Cancelled
            if isinstance(e, _Cancelled):
                status = "cancelled"
            else:
                status = "failed"
            code = getattr(e, "code", "internal") if isinstance(e, _AgentError) else "internal"
            outcome = ExecutorOutcome(
                status=status, error=ErrorInfo(code=code, message=str(e),
                                               retryable=bool(getattr(e, "retryable", False))))
        state.status = outcome.status
        state.result = outcome.answer
        state.artifacts = list(outcome.artifacts or state.artifacts)
        state.evidence = list(outcome.evidence or state.evidence)
        state.usage = outcome.usage or state.usage
        state.error = outcome.error or state.error
        state.pending = outcome.pending or state.pending

        # 内核验收端口：只在宿主给了 criteria 时启用，默认不改旧行为
        criteria = ((ctx.metadata or {}).get("verify_criteria")
                    or self.config.verify_criteria)
        if (self.config.verify_port is not None and criteria
                and state.status in ("ok", "partial")):
            try:
                verdict = self.config.verify_port.check(state.result, criteria, ctx)
                if not getattr(verdict, "passed", True):
                    reasons = "; ".join(getattr(verdict, "reasons", []) or []) \
                        or "验收未通过"
                    state.status = "failed"
                    state.error = ErrorInfo(code="verification_failed",
                                            message=reasons, retryable=True)
                    self._audit(ctx, "verify", "result", "failed", reasons)
                else:
                    self._audit(ctx, "verify", "result", "ok")
            except Exception as e:  # noqa: BLE001 —— 验收异常按失败处理
                state.status = "failed"
                state.error = ErrorInfo(code="verification_error",
                                        message=str(e), retryable=True)
                self._audit(ctx, "verify", "result", "error", str(e))

        if not state.ledger:
            from .domain import LedgerRow
            state.ledger.append(LedgerRow(
                run_id=ctx.run_id, node_id="main", objective=ctx.objective,
                status=state.status,
                digest=str(outcome.answer or "")[:20],
                tokens_used=state.usage.input_tokens + state.usage.output_tokens))
        try:
            self._commit(ctx, state, state.status)
        except Exception as e:  # noqa: BLE001 —— 存储失败要显式暴露
            from .errors import StoreError
            if isinstance(e, StoreError):
                raise
            raise StoreError(f"提交失败：{e}") from e
        self._audit(ctx, "run:done", ctx.objective, state.status,
                    state.error.message if state.error else "")
        self._checkpoint(ctx, "done", {
            "status": state.status,
            "answer": str(state.result or "")[:2000],
            "usage": {"input": state.usage.input_tokens,
                      "output": state.usage.output_tokens},
        })
        result = RunResult(
            run_id=ctx.run_id, trace_id=ctx.trace_id, status=state.status,
            answer=state.result, artifacts=state.artifacts,
            evidence=state.evidence, usage=state.usage, error=state.error,
            pending=state.pending, state=state)
        event_sink.emit(Event("done", {"status": state.status}, ctx.run_id, ctx.trace_id))
        if key:
            with self._lock:
                self._idempotent[key] = result
        return result

    def run(self, req: RunRequest) -> RunResult:
        return self._run(req)

    def stream(self, req: RunRequest):
        sink = _CollectSink()
        self._run(req, sink=sink)
        for ev in sink.events:
            yield ev

    def start(self, req: RunRequest):
        return RunHandle(self, req)

    def session(self, session_id: str):
        from .session_api import Session
        return Session(self, session_id)


class _CollectSink:
    def __init__(self):
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class RunHandle:
    """后台运行句柄；v1 用线程实现，Agent 本身线程安全。"""

    def __init__(self, agent: Agent, req: RunRequest):
        self.agent = agent
        self._token = req.cancel_token or CancelToken()
        self.req = replace(req, cancel_token=self._token)
        self._done = threading.Event()
        self._result: RunResult | None = None
        self._exc: BaseException | None = None
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self):
        try:
            self._result = self.agent.run(self.req)
        except BaseException as e:  # noqa: BLE001
            self._exc = e
        finally:
            self._done.set()

    def status(self) -> str:
        if self._done.is_set():
            return self._result.status if self._result else "failed"
        return "running"

    def result(self, timeout=None) -> RunResult:
        return self.wait(timeout)

    def wait(self, timeout=None) -> RunResult:
        if not self._done.wait(timeout):
            raise TimeoutError("RunHandle.wait 超时")
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result

    def cancel(self) -> None:
        self._token.cancel()

    def interrupt(self) -> None:
        self.cancel()

    def resume(self, input: Any) -> None:
        # v1 只支持“挂起后重开一轮”，不在同一线程内恢复
        if not self._done.is_set():
            raise ConfigError("运行尚未挂起")
        if self._result is None or self._result.pending is None:
            raise ConfigError("没有待恢复的挂起状态")
        meta = dict(self.req.metadata or {})
        meta["resume"] = input
        # 只有审批挂起才注入 approval_decision；其它挂起类型只带 resume
        if str(getattr(self._result.pending, "kind", "") or "") == "approval":
            pending_data = dict(self._result.pending.data or {})
            meta["resume_pending"] = dict(pending_data)
            meta["approval_decision"] = {**pending_data, "decision": input}
        self.req = replace(self.req, metadata=meta, cancel_token=CancelToken())
        self._token = self.req.cancel_token
        self._done.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
