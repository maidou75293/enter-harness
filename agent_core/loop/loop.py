"""单线程主循环：plan → act → observe。

特点（参照 Claude Code 的稳定内核）：
- 单线程、显式状态：循环体内只做三件事，不在回调里藏控制流。
- 停止条件明确：模型不再调用工具 / max_steps / timeout / interrupt。
- steering：`steer(text)` 追加指令，在下一步开始前注入（不打断正在执行的工具）。
- 中断：`interrupt()` 置位，在模型调用/工具边界检查；已产生的中间结果不丢。
- 可观测：每个阶段写结构化 trace（可回放）。
- 步数用尽时 `finalize`：禁用工具再问一次，避免“假失败”丢掉已获得的信息。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..contracts import ToolContext
from ..hooks.manager import HookManager
from ..runtime import current_run, current_session, current_tenant, current_user
from ..settings import as_settings
from ..tracing.tracer import Tracer


@dataclass
class RunResult:
    ok: bool = False
    answer: str = ""
    messages: list = field(default_factory=list)
    steps: int = 0
    trace: list = field(default_factory=list)
    stopped_reason: str = "final"
    interrupted: bool = False
    error: str = ""
    plan: dict = field(default_factory=dict)
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "answer": self.answer, "steps": self.steps,
            "trace": self.trace, "stopped_reason": self.stopped_reason,
            "interrupted": self.interrupted, "error": self.error,
            "plan": self.plan, "elapsed": self.elapsed,
            "messages": self.messages,
        }


class AgentLoop:
    def __init__(self, provider, tools, *, system_prompt: str = "", max_steps: int = 6,
                 timeout: float | None = 300, planner=None, hooks: HookManager | None = None,
                 tracer: Tracer | None = None, settings=None,
                 finalize_on_max_steps: bool = True,
                 on_event: Callable[[dict], Awaitable] | None = None):
        self.provider = provider
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_steps = int(max_steps)
        self.timeout = timeout
        self.planner = planner
        self.hooks = hooks or HookManager()
        self.tracer = tracer or Tracer()
        self.settings = as_settings(settings)
        self.finalize_on_max_steps = finalize_on_max_steps
        self.on_event = on_event
        self._interrupt = threading.Event()
        self._interrupt_reason = ""
        self._steer: deque[str] = deque()
        self._hints: set[str] = set()

    # ------------------------------------------------------------ 控制
    def interrupt(self, reason: str = "user") -> None:
        self._interrupt_reason = reason or "user"
        self._interrupt.set()

    def clear_interrupt(self) -> None:
        self._interrupt.clear()
        self._interrupt_reason = ""

    def steer(self, text: str) -> None:
        """运行中追加指令；在下一步开始前作为 user 消息注入。"""
        if text and str(text).strip():
            self._steer.append(str(text).strip())

    def clear_steering(self) -> None:
        self._steer.clear()

    @property
    def interrupted(self) -> bool:
        return self._interrupt.is_set()

    # ------------------------------------------------------------ 事件
    async def _emit(self, event_type: str, **payload) -> dict:
        ev = {"type": event_type, **payload}
        try:
            await self.tracer.emit(event_type, **payload)
        except Exception:  # noqa: BLE001
            pass
        if self.on_event:
            try:
                r = self.on_event(ev)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001
                pass
        return ev

    async def _check_stop(self, deadline: float) -> str | None:
        if self._interrupt.is_set():
            return "interrupted"
        if deadline and time.monotonic() > deadline:
            return "timeout"
        return None

    # ------------------------------------------------------------ 主循环
    async def run(self, task: str, messages: list | None = None,
                  ctx: ToolContext | dict | None = None) -> RunResult:
        started = time.monotonic()
        deadline = (started + self.timeout) if self.timeout else 0.0
        self._interrupt.clear()
        self._hints.clear()
        run_id = current_run.get() or f"loop-{int(started * 1000)}"
        msgs: list = list(messages or [])
        if not msgs or msgs[0].get("role") != "system":
            msgs.insert(0, {"role": "system", "content": self.system_prompt})
        msgs.append({"role": "user", "content": task})

        result = RunResult(messages=msgs)
        await self.hooks.emit("run_start", task=task, run=run_id)
        await self._emit("run_start", task=task, run=run_id)

        # ---- plan ----
        if self.planner is not None:
            try:
                plan = await self._make_plan(task, msgs, ctx)
                result.plan = plan or {}
                if plan:
                    await self._emit("plan", plan=plan)
            except Exception as e:  # noqa: BLE001 —— 规划失败降级为直接执行
                result.plan = {"error": f"{type(e).__name__}: {e}"}
                await self._emit("plan_error", error=result.plan["error"])

        stopped = None
        step = 0
        while step < self.max_steps:
            stop = await self._check_stop(deadline)
            if stop:
                stopped = stop
                break

            # steering：把运行中追加的指令插到当前对话末尾
            while self._steer:
                text = self._steer.popleft()
                msgs.append({"role": "user", "content": f"（运行中追加指令）{text}"})
                await self._emit("steer", text=text)

            # 连续同一调用失败 → 明确提示模型换方法（避免原样重试）
            hint = _failure_hint(msgs)
            if hint and hint not in self._hints:
                self._hints.add(hint)
                msgs.append({"role": "user", "content": "（系统提示）" + hint})
                await self._emit("failure_hint", hint=hint)

            step += 1
            await self.hooks.emit("step_start", step=step)
            await self._emit("step_start", step=step)
            await self.hooks.emit("model_start", step=step)
            await self._emit("model_start", step=step, round=step)

            try:
                msg = await self._chat(msgs, deadline)
            except asyncio.TimeoutError:
                stopped = "timeout"
                result.error = "模型调用超时"
                break
            except Exception as e:  # noqa: BLE001
                result.error = f"{type(e).__name__}: {e}"
                await self.hooks.emit("error", error=result.error, step=step)
                await self._emit("error", error=result.error, step=step)
                stopped = "error"
                break

            assistant = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                assistant["tool_calls"] = msg["tool_calls"]
            msgs.append(assistant)
            await self.hooks.emit("model_end", step=step, msg=assistant)
            await self._emit("model_end", step=step,
                             content=(assistant.get("content") or "")[:2000],
                             tool_calls=len(assistant.get("tool_calls") or []))

            calls = assistant.get("tool_calls") or []
            if not calls:
                result.answer = assistant.get("content") or ""
                stopped = "final"
                await self._emit("step_end", step=step, done=True)
                break

            # ---- act / observe ----
            parsed = []
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("参数必须是 JSON 对象")
                    err = ""
                except (json.JSONDecodeError, ValueError) as e:
                    args, err = {}, f"参数不是合法 JSON 对象（{e}）"
                parsed.append((call, name, args, err))

            base_ctx = ctx if isinstance(ctx, ToolContext) else ToolContext(
                tenant=current_tenant.get(), session=current_session.get(),
                run=run_id, user=current_user.get(), step=step)

            async def run_one(call, name, args, err):
                if err:
                    obs = (f"错误：工具 {name} 的{err}。收到的原始参数："
                           f"{str((call.get('function') or {}).get('arguments'))[:300]}。"
                           "请修正后重新调用。")
                    await self._emit("tool_call_error", name=name, error=err, step=step)
                    return {"ok": False, "error": obs}, obs
                tool_ctx = base_ctx if isinstance(ctx, ToolContext) else ToolContext(
                    tenant=base_ctx.tenant, session=base_ctx.session, run=run_id,
                    user=base_ctx.user, args=args, step=step,
                    subagent_depth=base_ctx.subagent_depth)
                tool_ctx.args = args
                tool_ctx.step = step
                await self._emit("tool_call", name=name, args=args, step=step)
                res = await self._execute(name, args, tool_ctx, deadline)
                return res, _observation(res)

            sem = asyncio.Semaphore(8)
            results: dict = {}

            async def run_capped(item):
                async with sem:
                    return await run_one(*item)

            async def flush(batch):
                if not batch:
                    return
                done = await asyncio.gather(*(run_capped(x) for x in batch))
                for item, (res, obs) in zip(batch, done):
                    results[id(item[0])] = (item, res, obs)

            batch = []
            for item in parsed:
                name = item[1]
                safe = (not item[3]) and hasattr(self.tools, "is_parallel_safe") \
                    and self.tools.is_parallel_safe(name)
                if safe:
                    batch.append(item)
                    continue
                await flush(batch)          # 先让只读批次完成，再执行写操作
                batch = []
                res, obs = await run_one(*item)
                results[id(item[0])] = (item, res, obs)
            await flush(batch)

            # 严格按模型给出的顺序回填工具消息
            for call, _n, _a, _e in parsed:
                item, res, obs = results[id(call)]
                msgs.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": obs})
                await self._emit("tool_observation", name=item[1],
                                 ok=bool(res.get("ok")), content=obs[:2000],
                                 step=step)
            await self._emit("step_end", step=step, done=False)

        # ---- 步数用尽 / 中断后的收尾 ----
        if stopped is None:
            stopped = "max_steps"
        if stopped == "interrupted":
            result.interrupted = True
            await self.hooks.emit("interrupt", reason=self._interrupt_reason)
            await self._emit("interrupt", reason=self._interrupt_reason)
        elif stopped == "max_steps" and self.finalize_on_max_steps:
            final = await self._finalize(msgs, deadline, result)
            if final:
                result.answer = final
                msgs.append({"role": "assistant", "content": final})

        if not result.answer:
            result.answer = _latest_assistant_text(msgs) or _latest_tool_text(msgs)
        result.steps = step
        result.stopped_reason = stopped
        result.ok = stopped == "final" and bool(result.answer.strip()) and not result.error
        result.messages = msgs
        result.elapsed = round(time.monotonic() - started, 3)
        await self._emit("final", answer=result.answer, stopped_reason=stopped,
                         steps=step, elapsed=result.elapsed)
        # final 本身也要进 trace（在快照之前写入）
        result.trace = self.tracer.to_dicts()
        await self.hooks.emit("run_end", result=result)
        return result

    # ------------------------------------------------------------ 内部
    async def _make_plan(self, task: str, msgs: list, ctx) -> dict:
        planner = self.planner
        if hasattr(planner, "plan"):
            maybe = planner.plan(task, context=msgs)
        else:
            maybe = planner(task, msgs)
        if asyncio.iscoroutine(maybe):
            maybe = await maybe
        return maybe if isinstance(maybe, dict) else {}

    def _aged(self, msgs: list) -> list:
        """工具输出老化：最近 N 条全文，更早的超长输出折叠为头+尾。"""
        keep = self.settings.get_int("loop.tool_age_after", 6)
        max_chars = self.settings.get_int("loop.tool_age_chars", 800)
        return age_tool_outputs(msgs, keep=keep, max_chars=max_chars)

    async def _chat(self, msgs: list, deadline: float) -> dict:
        tools = self.tools.openai_tools() if hasattr(self.tools, "openai_tools") else None
        remaining = (deadline - time.monotonic()) if deadline else None
        if remaining is not None and remaining <= 0:
            raise asyncio.TimeoutError()
        fut = self.provider.chat(self._aged(msgs), tools=tools)
        if remaining is not None:
            return await asyncio.wait_for(fut, timeout=remaining)
        return await fut

    async def _execute(self, name: str, args: dict, ctx: ToolContext,
                       deadline: float) -> dict:
        remaining = (deadline - time.monotonic()) if deadline else None
        if remaining is not None and remaining <= 0:
            return {"ok": False, "error": "运行超时"}
        try:
            fut = self.tools.execute(name, args, ctx=ctx)
            if remaining is not None:
                return await asyncio.wait_for(fut, timeout=remaining)
            return await fut
        except asyncio.TimeoutError:
            return {"ok": False, "error": "运行超时"}
        except Exception as e:  # noqa: BLE001 —— 工具错误留在上下文
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def _finalize(self, msgs: list, deadline: float, result: RunResult) -> str:
        """步数用尽：禁用工具再问一次，逼模型基于已有信息收尾。"""
        last = msgs[-1] if msgs else {}
        if last.get("role") == "assistant" and last.get("tool_calls"):
            # 放弃未执行的工具调用（部分服务要求 tool_calls 后必须跟 tool 消息）
            last = dict(last)
            last.pop("tool_calls", None)
            if not (last.get("content") or "").strip():
                last["content"] = "（放弃未执行的工具调用）"
            msgs[-1] = last
        msgs.append({"role": "user", "content":
                     "（系统提示）工具轮次已达上限，不要再调用任何工具；"
                     "请立即基于目前已获得的全部信息给出最终答复，并说明数据来源与局限。"})
        await self._emit("finalize", round=result.steps)
        try:
            remaining = (deadline - time.monotonic()) if deadline else None
            fut = self.provider.chat(self._aged(msgs), tools=[])
            msg = await (asyncio.wait_for(fut, timeout=remaining)
                         if remaining else fut)
            return (msg.get("content") or "").strip()
        except Exception as e:  # noqa: BLE001
            await self._emit("finalize_error", error=f"{type(e).__name__}: {e}")
            return ""


def age_tool_outputs(messages: list, keep: int = 6, max_chars: int = 800) -> list:
    """只改「发给模型」的视图：最近 keep 条工具输出保留，更早的折叠。"""
    if keep <= 0 or max_chars <= 0:
        return messages
    idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    old = set(idx[:-keep]) if len(idx) > keep else set()
    if not old:
        return messages
    out, half = [], max(100, max_chars // 2)
    for i, m in enumerate(messages):
        if i in old:
            s = str(m.get("content") or "")
            if len(s) > max_chars:
                m = dict(m)
                m["content"] = (s[:half]
                                + f"\n…[较早的工具输出已折叠，原长 {len(s)} 字符]…\n"
                                + s[-half:])
        out.append(m)
    return out


def _failure_hint(messages: list) -> str:
    """同一工具 + 相同参数连续失败 ≥2 次 → 提示模型换方法。"""
    calls: dict = {}
    last_sig, streak = None, 0
    for m in messages:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                calls[c.get("id")] = (fn.get("name"),
                                      str(fn.get("arguments") or ""))
        elif m.get("role") == "tool":
            sig = calls.get(m.get("tool_call_id"))
            failed = str(m.get("content") or "").lstrip().startswith("错误")
            if sig and failed:
                if sig == last_sig:
                    streak += 1
                else:
                    last_sig, streak = sig, 1
            else:
                last_sig, streak = None, 0
    if streak >= 2 and last_sig:
        return (f"工具 {last_sig[0]} 用相同参数已连续失败 {streak} 次。"
                "不要原样重试：请阅读报错，换参数/换工具/换方法后再试。")
    return ""


def _observation(res: dict) -> str:
    if res.get("ok"):
        return str(res.get("result"))
    if res.get("needs_approval"):
        return (f"工具 {res.get('tool') or ''} 需人工审批"
                f"（单号 {res.get('approval_id')}），本轮未执行。")
    return f"错误：{res.get('error')}"


def _latest_assistant_text(msgs: list) -> str:
    for m in reversed(msgs):
        if m.get("role") == "assistant" and not (m.get("tool_calls") or []):
            if str(m.get("content") or "").strip():
                return str(m.get("content"))
    return ""


def _latest_tool_text(msgs: list) -> str:
    for m in reversed(msgs):
        if m.get("role") == "tool" and str(m.get("content") or "").strip():
            return str(m.get("content"))[:800]
    return ""
