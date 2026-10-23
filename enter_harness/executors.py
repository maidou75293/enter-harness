"""v1.1 执行器转换层：ReAct / Graph / Hybrid 全部走 LangGraph。

- ReactExecutor    包装 `loop/react.py::build_react_subgraph`
- GraphExecutor    包装 `loop/graph.py::GraphEngine` + Planner / Verifier + 节点适配器
- HybridExecutor   GraphEngine，smart 节点内部走 react 子图
这里只做“端口 → LangGraph 组件”的转换与 RunContext/RunState 映射，
不再实现自研循环。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .domain import (
    ErrorInfo, Event, ExecutorOutcome, LedgerRow, Message, ModelResponse,
    PendingState, RunContext, RunState, Scope, ToolCall, ToolResult, Usage,
)
from .errors import AgentError, BudgetExceeded, CancelledError, ModelError, SchemaError
from .loop.graph import GraphEngine
from .loop.planner import Planner
from .loop.react import build_react_subgraph, react_final_text, react_has_final
from .loop.verifier import Verifier


def _run_async(coro):
    """同步 API 调 async LangGraph；若已在事件循环中，换线程执行。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def _to_message(m) -> Message:
    if isinstance(m, Message):
        return m
    if not isinstance(m, dict):
        return Message("user", str(m))
    calls = []
    for c in m.get("tool_calls") or []:
        if isinstance(c, ToolCall):
            calls.append(c)
            continue
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        calls.append(ToolCall(str(c.get("id") or ""), str(fn.get("name") or ""),
                              args if isinstance(args, dict) else {}))
    return Message(role=m.get("role") or "user", content=str(m.get("content") or ""),
                   tool_calls=tuple(calls), tool_call_id=str(m.get("tool_call_id") or ""),
                   name=str(m.get("name") or ""))


def _to_dict(m: Message) -> dict:
    return m.as_dict()


def _error(e: Exception) -> ErrorInfo:
    return ErrorInfo(code=getattr(e, "code", "internal"), message=str(e),
                     retryable=bool(getattr(e, "retryable", False)),
                     details=dict(getattr(e, "details", {}) or {}))


def _check_cancel(state) -> None:
    """在模型/工具边界检查取消标记（v1.1 CancelToken）。"""
    tok = getattr(state, "cancel_token", None)
    if tok is not None and getattr(tok, "cancelled", False):
        raise CancelledError("运行已取消")


def _compressed_view(messages: list[Message], summary: str, hooks) -> list[Message]:
    """压缩后的上下文表示：保留首条 system + 摘要 + 最近 keep 条。

    与 ``Compressor.apply`` 同步；摘要只影响“进上下文的表示”，原文仍由宿主保留。
    """
    keep = int(getattr(getattr(hooks, "compressor", None), "keep_recent", 4) or 4)
    head = [m for m in messages if m.role == "system"][:1]
    rest = [m for m in messages if m.role != "system"]
    return head + [Message("system", "历史摘要（原文可回查）：\n" + summary)] \
        + rest[-max(1, keep):]


class _LangProvider:
    """把同步 ModelPort 适配成 LangGraph react/planner/verifier 需要的 async chat。"""

    def __init__(self, model, ctx: RunContext, state: RunState, hooks=None):
        self.model = model
        self.ctx = ctx
        self.state = state
        self.hooks = hooks

    async def chat(self, messages, tools=None, tool_choice=None,
                   response_format=None, temperature=None, **kw):
        _check_cancel(self.state)
        if self.hooks:
            self.hooks.before_model_call(self.ctx, self.state)
        msgs = [_to_message(m) for m in messages]
        if self.state.summary:
            msgs = _compressed_view(msgs, self.state.summary, self.hooks)
        try:
            resp: ModelResponse = self.model.complete(msgs, tools or [], self.ctx.budget)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, AgentError):
                raise
            raise ModelError(f"模型调用失败：{type(e).__name__}: {e}") from e
        self.state.usage = self.state.usage.merge(
            resp.usage if resp.usage is not None else Usage())
        out: dict[str, Any] = {"role": "assistant", "content": resp.content or ""}
        if resp.tool_calls:
            out["tool_calls"] = [{
                "id": c.id, "type": "function",
                "function": {"name": c.name,
                             "arguments": json.dumps(c.arguments, ensure_ascii=False)},
            } for c in resp.tool_calls]
        return out

    async def complete(self, messages, **kw):
        resp = self.model.complete([_to_message(m) for m in messages], [], self.ctx.budget)
        self.state.usage = self.state.usage.merge(resp.usage or Usage())
        return resp.content or ""


class _LangToolBus:
    """把同步 ToolRegistry + ToolPolicyPort 适配成 react 子图需要的 async 工具总线。"""

    def __init__(self, registry, ctx: RunContext, state: RunState, hooks=None):
        self.registry = registry
        self.ctx = ctx
        self.state = state
        self.hooks = hooks

    def list_tools(self):
        return [{"name": t.name, "description": t.description,
                 "parameters": t.parameters}
                for t in self.registry.list_tools(self.ctx.tool_whitelist or None)]

    def has(self, name):
        """Planner.validate 需要的能力：判断（白名单内的）工具是否存在。"""
        return any(t["name"] == name for t in self.list_tools())

    async def execute(self, name, args, ctx=None):
        _check_cancel(self.state)
        args = args if isinstance(args, dict) else {}
        if self.ctx.tool_whitelist and name not in self.ctx.tool_whitelist:
            self._audit(f"tool:{name}", name, "denied", "不在工具白名单")
            return {"ok": False, "error": f"工具不在白名单：{name}"}
        policy = (self.ctx.metadata or {}).get("policy")
        if policy is not None:
            d = policy.check(self.ctx, {"tool": name, "args": args})
            decision = getattr(d, "decision", "allow")
            reason = getattr(d, "reason", "")
            if decision == "deny":
                self._audit(f"tool:{name}", name, "denied", reason or "权限拒绝")
                return {"ok": False, "denied": True,
                        "error": reason or "权限拒绝"}
            if decision == "ask":
                approval_id = str(getattr(d, "approval_id", "") or "")
                pending = dict((self.ctx.metadata or {}).get("approval_decision")
                               or {})
                if self._matches(pending, name, args, approval_id):
                    approved = self._truthy(pending.get("decision"))
                    self._audit(f"approval:{name}", name,
                                "approved" if approved else "denied",
                                reason or str(pending.get("reason") or ""))
                    if not approved:
                        self._checkpoint(f"approval:{name}", {
                            "tool": name, "args": args, "ok": False,
                            "approval_id": approval_id, "reason": "rejected"})
                        return {"ok": False, "denied": True, "error": "审批被拒绝"}
                    # 通过：继续走下面的工具执行
                else:
                    approval_id = approval_id or ("ap_" + uuid.uuid4().hex[:12])
                    prompt = reason or f"工具 {name} 需要审批"
                    self._queue_ticket(name, args, approval_id, prompt)
                    self.state.pending = PendingState(
                        kind="approval", prompt=prompt,
                        data={"tool": name, "args": args,
                              "approval_id": approval_id})
                    self._audit(f"approval:{name}", name, "pending", prompt)
                    self._checkpoint(f"approval:{name}", {
                        "tool": name, "args": args, "ok": False,
                        "approval_id": approval_id, "reason": "pending"})
                    return {"ok": False, "needs_approval": True,
                            "approval_id": approval_id, "error": prompt}
        res = self.registry.execute(name, args, self.ctx)
        self.state.tool_results.append(ToolResult(
            call_id="", name=name, ok=res.ok, content=res.content,
            error=res.error, metadata=res.metadata))
        self._audit(f"tool:{name}", name, "ok" if res.ok else "error",
                    res.error or "")
        self._checkpoint(f"tool:{name}", {
            "tool": name, "args": args, "ok": bool(res.ok),
            "error": res.error or "", "step": self.state.step})
        if self.hooks:
            self.hooks.after_tool_batch(self.ctx, self.state)
            self._maybe_pressure()
        return {"ok": res.ok, "result": res.content, "error": res.error}

    # ------------------------------------------------------------ 审批 / 观测
    @staticmethod
    def _truthy(value) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in ("1", "true", "yes", "ok", "allow", "approved",
                        "approve", "通过", "同意")

    @staticmethod
    def _matches(pending: dict, name: str, args: dict,
                 approval_id: str) -> bool:
        if not pending:
            return False
        pid = str(pending.get("approval_id") or "")
        # 双方都有 id 时必须一致；政策没给 id（首次是内核生成的）时回退工具+参数
        if approval_id and pid and approval_id != pid:
            return False
        return (str(pending.get("tool") or "") == name
                and dict(pending.get("args") or {}) == dict(args or {}))

    def _queue_ticket(self, name: str, args: dict, approval_id: str,
                      prompt: str) -> None:
        queue = (self.ctx.metadata or {}).get("approval_queue")
        if queue is None:
            return
        try:
            from .harness import ApprovalTicket
            queue.add(ApprovalTicket(
                id=approval_id,
                tenant_id=str(getattr(self.ctx.scope, "tenant_id", "") or ""),
                actor=str(getattr(self.ctx.scope, "user_id", "") or ""),
                tool=name, payload={"args": args, "ctx": {"tool": name}},
                reason=prompt, status="pending"))
        except Exception:  # noqa: BLE001 —— 队列失败仍返回挂起状态
            pass

    def _audit(self, action: str, target: str, decision: str,
               reason: str = "") -> None:
        sink = (self.ctx.metadata or {}).get("audit")
        if sink is None:
            return
        try:
            from .harness import AuditEvent
            sink.emit(AuditEvent(
                tenant_id=str(getattr(self.ctx.scope, "tenant_id", "") or ""),
                actor=str(getattr(self.ctx.scope, "user_id", "") or ""),
                action=action, target=str(target)[:200], decision=decision,
                reason=str(reason)[:500], trace_id=self.ctx.trace_id))
        except Exception:  # noqa: BLE001
            pass

    def _checkpoint(self, node: str, payload: dict) -> None:
        store = (self.ctx.metadata or {}).get("state_port")
        if store is None:
            return
        try:
            store.checkpoint(self.ctx.run_id, node, dict(payload or {}))
        except Exception:  # noqa: BLE001
            pass

    def _maybe_pressure(self):
        if not self.hooks:
            return
        try:
            used = self.model.count_tokens(self.state.messages)
        except Exception:
            used = 0
        if used and used > int(self.ctx.budget.max_tokens * 0.8):
            self.hooks.on_context_pressure(self.ctx, self.state)

    @property
    def model(self):
        return self.ctx.metadata["model"]


def _event_sink(ctx):
    return (ctx.metadata or {}).get("event_sink")


class ReactExecutor:
    """包装现有 LangGraph react 子图；不自研循环。"""

    def execute(self, ctx: RunContext, state: RunState) -> ExecutorOutcome:
        return _run_async(self._execute(ctx, state))

    async def _execute(self, ctx: RunContext, state: RunState) -> ExecutorOutcome:
        hooks = (ctx.metadata or {}).get("hooks")
        sink = _event_sink(ctx)
        provider = _LangProvider(ctx.metadata["model"], ctx, state, hooks)
        tools = _LangToolBus(ctx.tools, ctx, state, hooks)

        async def on_step(info):
            state.step = max(state.step, int(info.get("round") or 0))
            if sink:
                sink.emit(Event("model", {"round": info.get("round"),
                                          "max": info.get("max"),
                                          "tools": info.get("tools") or []},
                                ctx.run_id, ctx.trace_id))

        initial = [_to_dict(m) for m in state.messages]
        extra_system = str((ctx.metadata or {}).get("extra_system") or "")
        selector = (ctx.metadata or {}).get("tool_selector")
        try:
            sub = build_react_subgraph(
                provider, tools, max_steps=max(1, int(ctx.budget.max_steps)),
                extra_system=extra_system, tool_ctx=ctx, on_step=on_step,
                tool_selector=selector)
            final = await sub.ainvoke(
                {"messages": initial, "durations": [], "steps": 0})
        except CancelledError as e:
            state.status = "cancelled"
            state.error = _error(e)
            if hooks:
                hooks.on_cancel(ctx, state)
            return ExecutorOutcome("cancelled", error=state.error, usage=state.usage)
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, AgentError):
                e = ModelError(f"ReAct 执行失败：{type(e).__name__}: {e}")
            state.status = "failed"
            state.error = _error(e)
            if hooks:
                hooks.before_finish(ctx, state)
            return ExecutorOutcome("failed", error=state.error, usage=state.usage)

        msgs = final.get("messages") or []
        state.messages = [_to_message(m) for m in msgs]
        state.step = int(final.get("steps") or state.step)
        # finalize 节点只在步数用尽时进入，且会把 steps +1；据此判定预算耗尽。
        hit_step_limit = state.step > max(1, int(ctx.budget.max_steps))
        pending = final.get("pending") or {}
        if pending:
            state.pending = state.pending or PendingState(
                kind="approval", prompt="工具需要审批",
                data=dict(pending))
            state.status = "blocked"
            if hooks:
                hooks.before_finish(ctx, state)
            return ExecutorOutcome("blocked", pending=state.pending,
                                   usage=state.usage)
        answer = react_final_text(msgs)
        if state.summary:
            # 本轮发生过压缩：RunState 暴露压缩后的上下文表示，而不是完整原文。
            state.messages = _compressed_view(state.messages, state.summary, hooks)
        ok = react_has_final(msgs) and not hit_step_limit
        state.result = answer
        state.status = "ok" if ok else "failed"
        if not ok:
            e = BudgetExceeded(f"ReAct 步数用尽（{ctx.budget.max_steps}）"
                               if hit_step_limit else "ReAct 未产出最终答复")
            state.error = _error(e)
        if hooks:
            hooks.before_finish(ctx, state)
        return ExecutorOutcome(state.status, answer=answer, usage=state.usage,
                               error=state.error)


# ---------------------------------------------------------------- Graph
def _graph_plan(graph) -> dict:
    nodes, ids = [], set()
    for n in graph.nodes:
        if not n.node_id or n.node_id in ids:
            raise SchemaError(f"图节点 id 非法或重复：{n.node_id}")
        ids.add(n.node_id)
    for n in graph.nodes:
        for d in n.depends_on or ():
            if d not in ids:
                raise SchemaError(f"节点 {n.node_id} 依赖不存在：{d}")
        nodes.append({
            "id": n.node_id, "title": n.node_id,
            "type": "deterministic" if n.type == "tool" else "smart",
            "deps": list(n.depends_on or ()),
            "tool": n.tool or None, "args": dict(n.args or {}),
            "goal": n.prompt or n.node_id,
            "v11_type": n.type,
        })
    # 环检测
    by_id = {n["id"]: n for n in nodes}
    seen = {}

    def visit(nid):
        if seen.get(nid) == 1:
            raise SchemaError(f"图存在环：{nid}")
        if seen.get(nid) == 2:
            return
        seen[nid] = 1
        for d in by_id[nid]["deps"]:
            visit(d)
        seen[nid] = 2

    for nid in by_id:
        visit(nid)
    final = graph.final_node or nodes[-1]["id"]
    if final not in by_id:
        final = nodes[-1]["id"]
    return {"goal": "", "nodes": nodes, "final_node": final}


class _CoreNodeExecutor:
    """GraphEngine 的节点适配器：tool / llm / react 三类节点。"""

    def __init__(self, ctx, state, provider, tools, *, force_react=False):
        self.ctx, self.state = ctx, state
        self.provider, self.tools = provider, tools
        self.force_react = force_react

    async def run(self, node, gstate, on_event=None):
        _check_cancel(self.state)
        nid = node["id"]
        deps_text = "\n".join(
            f"[{d}] {gstate.get('results', {}).get(d, '')}" for d in node["deps"])
        goal = node.get("goal") or nid
        vtype = node.get("v11_type") or ("tool" if node["type"] == "deterministic" else "llm")
        if vtype == "tool":
            args = node.get("args") or {}
            res = await self.tools.execute(node.get("tool"), args)
            output = str(res.get("result") or "")
            ok = bool(res.get("ok"))
            if not ok:
                raise RuntimeError(res.get("error") or "工具失败")
            self._ledger(nid, goal, output, ok)
            return {"output": output, "ok": ok, "events": [
                {"type": "node_done", "id": nid, "ok": ok,
                 "tool": node.get("tool"), "args": args,
                 "output": output[:4000]}]}
        extra_system = str((self.ctx.metadata or {}).get("extra_system") or "")
        selector = (self.ctx.metadata or {}).get("tool_selector")
        if vtype == "react" or (vtype == "llm" and self.force_react):
            sub = build_react_subgraph(
                self.provider, self.tools,
                extra_system=extra_system,
                max_steps=max(1, int(self.ctx.budget.max_steps)),
                tool_selector=selector)
            final = await sub.ainvoke({"messages": [
                {"role": "user", "content": goal + (("\n" + deps_text) if deps_text else "")}
            ], "durations": [], "steps": 0})
            output = react_final_text(final.get("messages") or [])
            ok = react_has_final(final.get("messages") or [])
            self._ledger(nid, goal, output, ok)
            return {"output": output, "ok": ok, "events": [
                {"type": "node_done", "id": nid, "ok": ok,
                 "kind": "react", "output": output[:4000]}]}
        msgs = ([{"role": "system", "content": extra_system}]
                if extra_system else [])
        msgs.append({"role": "user",
                     "content": goal + (("\n" + deps_text) if deps_text else "")})
        msg = await self.provider.chat(msgs)
        output = msg.get("content") or ""
        self._ledger(nid, goal, output, True)
        return {"output": output, "ok": True, "events": [
            {"type": "node_done", "id": nid, "ok": True,
             "kind": "llm", "output": output[:4000]}]}

    def _ledger(self, nid, goal, output, ok):
        self.state.ledger.append(LedgerRow(
            run_id=self.ctx.run_id, node_id=nid, objective=goal,
            status="ok" if ok else "failed", digest=str(output)[:20],
            tokens_used=self.state.usage.input_tokens + self.state.usage.output_tokens))


class GraphExecutor:
    """包装 GraphEngine + Planner/Verifier；不实现图循环。"""

    force_react = False

    def execute(self, ctx: RunContext, state: RunState) -> ExecutorOutcome:
        return _run_async(self._execute(ctx, state))

    async def _execute(self, ctx: RunContext, state: RunState) -> ExecutorOutcome:
        hooks = (ctx.metadata or {}).get("hooks")
        provider = _LangProvider(ctx.metadata["model"], ctx, state, hooks)
        tools = _LangToolBus(ctx.tools, ctx, state, hooks)
        graph = (ctx.metadata or {}).get("graph")
        try:
            if graph is not None and graph.nodes:
                plan = _graph_plan(graph)
            else:
                planner = Planner(provider, None, tools)
                plan = await planner.plan(ctx.objective)
                if not plan.get("nodes"):
                    raise SchemaError("Planner 未返回任务图")
            executor = _CoreNodeExecutor(ctx, state, provider, tools,
                                         force_react=self.force_react)
            checkpoint = (ctx.metadata or {}).get("checkpoint_path") or str(
                Path(tempfile.gettempdir()) / f"enter_harness_{ctx.run_id}.db")
            engine = GraphEngine(store=None, checkpoint_path=checkpoint)

            async def on_event(ev):
                sink = _event_sink(ctx)
                if sink:
                    sink.emit(Event("node", ev, ctx.run_id, ctx.trace_id))

            result = await engine.run(
                plan, executor, thread_id=ctx.run_id,
                session_id=str((ctx.metadata or {}).get("session_id") or ""),
                on_event=on_event)
            results = result.get("results") or {}
            final_id = plan.get("final_node")
            answer = results.get(final_id) or next(iter(results.values()), "")
            verifier = Verifier(provider)
            verdict = await verifier.verify(ctx.objective, plan, results, mode="rules")
            state.result = answer
            state.status = "ok" if verdict.get("ok") else "failed"
            if not verdict.get("ok"):
                state.error = ErrorInfo(
                    "verification_failed", str(verdict.get("feedback") or "校验未通过"))
            if hooks:
                hooks.before_finish(ctx, state)
            return ExecutorOutcome(state.status, answer=answer, usage=state.usage,
                                   error=state.error)
        except CancelledError as e:
            state.status = "cancelled"
            state.error = _error(e)
            if hooks:
                hooks.on_cancel(ctx, state)
            return ExecutorOutcome("cancelled", error=state.error, usage=state.usage)
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, AgentError):
                e = SchemaError(f"图执行失败：{type(e).__name__}: {e}")
            state.status = "failed"
            state.error = _error(e)
            if hooks:
                hooks.before_finish(ctx, state)
            return ExecutorOutcome("failed", error=state.error, usage=state.usage)


class HybridExecutor(GraphExecutor):
    """GraphEngine + 节点内 react 子图。"""

    force_react = True
