"""Orchestrator（内部阶段，非公开入口）：plan → 动态建图 → 执行 → 校验 → 记忆写入。

v1.1 起公开入口只有 ``Agent.run / stream / start``；图执行由
``executors.GraphExecutor`` 包装 ``GraphEngine + Planner/Verifier`` 完成。
本模块保留为旧异步管线，不再从 ``agent_core`` 导出，避免两个入口并存。

业务差异通过构造参数注入：
- planner_system / verifier_system：业务提示词
- on_approval_pending：审批挂起时的持久化回调（core 不碰业务表）
- checkpoint_path：图状态落盘位置
"""
from __future__ import annotations

import inspect
import time

from ..runtime import current_run
from ..tracing.tracer import Tracer
from ..settings import as_settings
from .graph import GraphEngine
from .node_executor import NodeExecutor
from .planner import Planner
from .verifier import Verifier


class Orchestrator:
    def __init__(self, store, provider, tools, memory, skills, sessions, context_mgr,
                 *, planner: Planner | None = None, verifier: Verifier | None = None,
                 engine: GraphEngine | None = None, tracer: Tracer | None = None,
                 on_approval_pending=None, planner_system: str | None = None,
                 verifier_system: str | None = None, checkpoint_path=None,
                 tool_context=None):
        self.store = store
        self.settings = as_settings(store)
        self.provider = provider
        self.tools = tools
        self.memory = memory
        self.skills = skills
        self.sessions = sessions
        self.context = context_mgr
        self.tracer = tracer
        self.on_approval_pending = on_approval_pending
        self.tool_context = tool_context
        self.planner = planner or Planner(provider, store, tools,
                                          system_prompt=planner_system)
        self.engine = engine or GraphEngine(store, checkpoint_path=checkpoint_path)
        self.verifier = verifier or Verifier(provider, system_prompt=verifier_system)

    async def _cap_answer(self, answer) -> str:
        n = self.settings.get_int("orchestrator.answer_max_chars", 1000)
        s = str(answer or "")
        if n <= 0 or len(s) <= n:
            return s
        try:
            out = await self.provider.complete([{"role": "user", "content":
                f"把下面的内容归纳成不超过 {n} 字的中文要点，保留关键结论、数据与信息来源，"
                f"不要遗漏重要信息，直接输出归纳结果：\n\n{s[:8000]}"}])
            if out and out.strip():
                return out.strip()
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 最终答复归纳失败，改为截断：{e}")
        return s[:n] + f"…（已截断，原文 {len(s)} 字）"

    async def handle(self, sid: str, user_text: str, on_event=None,
                     seed_results=None) -> dict:
        started = time.time()
        run_id = f"{sid}-{int(started)}"
        try:
            current_run.set(run_id)
        except Exception:  # noqa: BLE001
            pass
        stream = []
        _pending = []
        _seq = 0

        def _flush_log():
            nonlocal _seq, _pending
            if not _pending:
                return
            try:
                _seq = self.store.append_run_events_batch(run_id, sid, _seq + 1, _pending)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 运行日志批量写入失败：{e}")
                _seq += len(_pending)
            _pending = []

        def emit(ev):
            ev = dict(ev)
            ev["t"] = round(time.time() - started, 3)
            stream.append(ev)
            _pending.append(ev)
            if self.tracer is not None:
                try:
                    self.tracer.record(str(ev.get("type") or "event"), **ev)
                except Exception:  # noqa: BLE001
                    pass
            if len(_pending) >= 5:
                _flush_log()
            return ev

        async def emit_async(ev):
            e = emit(ev)
            if on_event:
                await on_event(e)

        # 1) 上下文组装
        ctx = await self.context.build(sid, user_text, self.provider)
        await emit_async({"type": "context", "history_len": ctx["history_len"],
                          "injected_memory": ctx["injected_memory"],
                          "preview": ctx.get("preview"),
                          "compaction": ctx["compaction"]})

        # 2) Planner
        t_plan = time.time()
        plan = await self.planner.plan(user_text, context=ctx["messages"])
        await emit_async({"type": "plan", "plan": plan,
                          "dur": round(time.time() - t_plan, 3)})

        # 3) 执行 + 校验（最多 max_replans 次重规划）
        max_replans = self.settings.get_int("orchestrator.max_replans", 2)
        attempts = []
        result = None
        verification = None
        prev_plan = None
        prev_results = {}
        pending_answer = None
        for attempt in range(max_replans + 1):
            executor = NodeExecutor(self.provider, self.tools, self.store,
                                    tool_ctx=self.tool_context)
            executor.sys_text = ctx["messages"][0]["content"] if ctx.get("messages") else ""
            executor.history = ctx["messages"][1:-1] if ctx.get("messages") else []
            thread_id = f"{run_id}-a{attempt}"
            reuse = {}
            if seed_results:
                reuse = dict(seed_results)
                seed_results = None
            if attempt > 0 and prev_plan:
                prev_by_id = {n["id"]: n for n in prev_plan.get("nodes", [])}
                for n in plan["nodes"]:
                    p = prev_by_id.get(n["id"])
                    if not p:
                        continue
                    same = (p.get("title"), p.get("goal"), p.get("type")) == \
                           (n.get("title"), n.get("goal"), n.get("type"))
                    out = prev_results.get(n["id"])
                    if same and out is not None and not str(out).startswith("[失败]"):
                        reuse[n["id"]] = out
            await emit_async({"type": "graph_start", "attempt": attempt,
                              "thread_id": thread_id,
                              "nodes": [n["id"] for n in plan["nodes"]],
                              "reused": sorted(reuse.keys())})
            result = await self.engine.run(plan, executor, thread_id, session_id=sid,
                                           on_event=emit_async, reuse=reuse)
            # 审批挂起：不继续跑，记下现场等管理员审核
            _pa = (self.tools.pop_pending_approval()
                   if hasattr(self.tools, "pop_pending_approval") else None)
            if _pa:
                info = {"session_id": sid, "requirement": user_text, "plan": plan,
                        "results": result.get("results") or {},
                        "approval_id": _pa["approval_id"],
                        "tool": _pa.get("tool"), "scope": _pa.get("scope"),
                        "run_id": run_id}
                if self.on_approval_pending:
                    try:
                        r = self.on_approval_pending(info)
                        if inspect.isawaitable(r):
                            await r
                    except Exception as _e:  # noqa: BLE001
                        print(f"[warn] 审批挂起记录失败：{_e}")
                await emit_async({"type": "approval_wait",
                                  "approval_id": _pa.get("approval_id"),
                                  "tool": _pa.get("tool"), "scope": _pa.get("scope")})
                pending_answer = (f"⏳ 已提交审核，等待审核（单号 {_pa.get('approval_id')}）。"
                                  f"管理员批准后，本任务会自动继续执行。")
                break
            prev_plan, prev_results = plan, result["results"]
            vmode = (self.settings.get_config("orchestrator.verify_mode", "rules")
                     or "rules")
            t_verify = time.time()
            verification = await self.verifier.verify(user_text, plan, result["results"],
                                                      mode=vmode)
            attempts.append({"attempt": attempt, "thread_id": thread_id,
                             "verification": verification})
            await emit_async({"type": "verify", "attempt": attempt,
                              "ok": verification["ok"],
                              "feedback": verification["feedback"],
                              "dur": round(time.time() - t_verify, 3)})
            if verification["ok"] or attempt >= max_replans:
                break
            plan = await self.planner.replan(user_text, plan, verification["feedback"],
                                             context=ctx["messages"])
            await emit_async({"type": "replan", "attempt": attempt + 1, "plan": plan})

        # 4) 汇总最终答复
        if pending_answer:
            answer = pending_answer
            final_id = plan.get("final_node")
        else:
            final_id = plan.get("final_node")
            answer = result["results"].get(final_id) or next(
                iter(result["results"].values()), "(无输出)")
            answer = await self._cap_answer(answer)
        await emit_async({"type": "answer", "final_node": final_id, "answer": answer})

        # 5) 记忆写入（策展）—— 默认关闭自动记录
        if (self.settings.get_config("memory.auto_write", "off") or "off") == "on":
            mem = self.memory.write(f"用户需求：{user_text}", type="事实", source="session",
                                    confidence=0.5, importance=0.4)
        else:
            mem = {"action": "skipped（自动记录已关闭）"}
        await emit_async({"type": "memory_write", "result": mem})

        record = {
            "run_id": run_id,
            "requirement": user_text,
            "plan": plan,
            "results": result["results"],
            "verification": verification,
            "attempts": attempts,
            "answer": answer,
            "stream": stream,
            "elapsed": round(time.time() - started, 3),
            "context": {"injected_memory": ctx["injected_memory"],
                        "compaction": ctx["compaction"]},
        }
        self.sessions.add(sid, "assistant", answer,
                          meta={"run_id": run_id, "final_node": final_id})
        _flush_log()
        return record
