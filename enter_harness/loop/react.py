"""ReAct 子图（智能节点内部执行）。

图形态：START → agent →(有 tool_calls？) tools → agent → … → END
- agent 节点：调模型（带 tools），产出 assistant 消息（content 或 tool_calls）
- tools 节点：执行 tool_calls，结果以 role="tool" 消息写回
- 步数用尽 → finalize 节点：禁用工具再问一次，强逼模型基于已有信息给最终答复

走主流原生 tool calling，不解析模型打印的 JSON 文本。
"""
from __future__ import annotations

import json
import operator
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

DEFAULT_REACT_SYSTEM = """你是执行器：按目标逐步调用工具，直到完成、或确实做不到。
- 目标是改变系统状态时，必须真正调用工具执行；只说明步骤等于没做。
- 遇到报错先分析原因再换方法重试，不要原样重发同一调用。
- 不猜/爆破密码，不翻找凭据；缺权限就如实报告。
- 中间步骤与工具结果用户看不到，最终答复必须自带完整结论。
- 有副作用的操作先探测现状，已存在就复用，禁止重复启动。
- 上下文里的「已加载技能」中的铁律/禁止项是硬约束。"""


def tool_specs(tools) -> list:
    """工具总线 → OpenAI tools 格式。"""
    out = []
    for t in tools.list_tools():
        out.append({"type": "function", "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("parameters") or {"type": "object", "properties": {}},
        }})
    return out


class ReactState(TypedDict):
    messages: Annotated[list, operator.add]
    durations: Annotated[list, operator.add]
    steps: int
    pending: dict


def _last_user_text(messages: list, limit: int = 4000) -> str:
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c[:limit]
        return ""
    return ""


def build_react_subgraph(provider, tools, extra_system: str = "", max_steps: int = 6,
                         on_step=None, base_system: str | None = None,
                         tool_ctx=None, tool_selector=None):
    """构建并编译 ReAct 子图。

    extra_system: 业务层追加的系统提示（技能、工作手册、最近上下文等）。
    base_system:  覆盖 core 默认执行器提示。
    tool_ctx:     传给 tools.execute 的上下文（租户/会话/运行）。
    """
    system = (DEFAULT_REACT_SYSTEM if base_system is None else base_system)
    if extra_system:
        system = system + "\n\n" + extra_system
    specs = tool_specs(tools)
    loaded: set = set()

    async def agent(state: ReactState):
        msgs = [{"role": "system", "content": system}] + state["messages"]
        turn_specs = specs
        if tool_selector is not None:
            try:
                metas = tools.list_tools()
                sel = tool_selector(_last_user_text(state["messages"]),
                                    sorted(loaded), metas) or {}
                names = sel.get("selected")
                if names:
                    allow = {str(x) for x in names}
                    turn_specs = [s for s in specs
                                  if s.get("function", {}).get("name") in allow]
                directory = str(sel.get("directory") or "")
                if directory:
                    msgs.append({"role": "system", "content": directory})
            except Exception:  # noqa: BLE001 —— 路由失败退回全量，不阻断本轮
                turn_specs = specs
        t0 = time.time()
        msg = await provider.chat(msgs, tools=turn_specs)
        ms = int((time.time() - t0) * 1000)
        keep = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            keep["tool_calls"] = msg["tool_calls"]
        rnd = state.get("steps", 0) + 1
        if on_step:
            names = [((c.get("function") or {}).get("name"))
                     for c in (msg.get("tool_calls") or [])]
            try:
                await on_step({"round": rnd, "max": max_steps,
                               "tools": [n for n in names if n]})
            except Exception:  # noqa: BLE001
                pass
        return {"messages": [keep], "durations": [{"who": "agent", "ms": ms}],
                "steps": rnd}

    async def call_tools(state: ReactState):
        last = state["messages"][-1]
        out, dur = [], []
        pend = {}
        for c in (last.get("tool_calls") or []):
            fn = c.get("function") or {}
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if isinstance(args, dict) and name == "tool_load":
                _names = args.get("names") or args.get("name") or []
                if isinstance(_names, str):
                    _names = [x.strip() for x in _names.split(",") if x.strip()]
                if isinstance(_names, list):
                    loaded.update(str(x).strip() for x in _names[:20]
                                  if str(x).strip())
            t0 = time.time()
            try:
                res = await tools.execute(name, args, ctx=tool_ctx)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if res.get("ok"):
                obs = str(res.get("result"))
            elif res.get("needs_approval"):
                obs = (f"工具 {name} 需人工审批（单号 {res.get('approval_id')}），本轮未执行。")
                pend = {"approval_id": res.get("approval_id"), "tool": name}
            else:
                obs = f"错误：{res.get('error')}"
            dur.append({"who": "tool", "ms": int((time.time() - t0) * 1000)})
            out.append({"role": "tool", "tool_call_id": c.get("id"), "content": obs})
        return {"messages": out, "durations": dur, "pending": pend}

    def route_tools(state: ReactState):
        if state.get("pending"):
            return END
        return "agent"

    def route(state: ReactState):
        last = state["messages"][-1]
        if last.get("role") == "assistant" and (last.get("tool_calls") or []):
            return "finalize" if state.get("steps", 0) >= max_steps else "tools"
        return END

    async def finalize(state: ReactState):
        msgs = [{"role": "system", "content": system}] + [dict(m) for m in state["messages"]]
        if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
            msgs[-1].pop("tool_calls", None)
            if not str(msgs[-1].get("content") or "").strip():
                msgs[-1]["content"] = "（放弃未执行的工具调用）"
        msgs.append({"role": "user", "content":
                     "（系统提示）工具调用轮次已达上限，**不要再调用任何工具**；"
                     "请立即基于目前已获得的全部信息给出最终答复，并说明数据来源与局限。"})
        t0 = time.time()
        msg = await provider.chat(msgs, tools=[])
        ms = int((time.time() - t0) * 1000)
        content = (msg.get("content") or "").strip()
        if not content:
            last_obs = next((str(m.get("content")) for m in reversed(state["messages"])
                             if m.get("role") == "tool"
                             and str(m.get("content") or "").strip()), "")
            content = ("（未生成完整结论，以下为已获取内容的摘要）\n" + last_obs[:800]
                       ) if last_obs else ""
        keep = {"role": "assistant", "content": content}
        return {"messages": [keep], "durations": [{"who": "agent", "ms": ms}],
                "steps": state.get("steps", 0) + 1}

    g = StateGraph(ReactState)
    g.add_node("agent", agent)
    g.add_node("tools", call_tools)
    g.add_node("finalize", finalize)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route,
                            {"tools": "tools", "finalize": "finalize", END: END})
    g.add_conditional_edges("tools", route_tools, {"agent": "agent", END: END})
    g.add_edge("finalize", END)
    return g.compile()


def react_final_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant" and not (m.get("tool_calls") or []):
            return m.get("content") or ""
    return ""


def react_has_final(messages: list) -> bool:
    for m in reversed(messages):
        if m.get("role") == "assistant" and not (m.get("tool_calls") or []):
            return bool((m.get("content") or "").strip())
    return False


def react_trace(messages: list, durations: list | None = None) -> list:
    durations = durations or []
    out = []
    di = 0
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            ms = durations[di]["ms"] if di < len(durations) else None
            di += 1
            if m.get("tool_calls"):
                for c in m["tool_calls"]:
                    fn = c.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {"_raw": fn.get("arguments")}
                    out.append({"step": "action", "thought": m.get("content") or "",
                                "tool": fn.get("name"), "args": args, "ms": ms})
            elif (m.get("content") or "").strip():
                out.append({"step": "final", "thought": "",
                            "final": m.get("content"), "ms": ms})
        elif role == "tool":
            ms = durations[di]["ms"] if di < len(durations) else None
            di += 1
            out.append({"step": "observation",
                        "content": str(m.get("content"))[:2000], "ms": ms})
    return out
