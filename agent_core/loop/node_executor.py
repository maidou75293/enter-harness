"""Node Executor：执行任务图节点。

两类节点：
- deterministic：直接调工具（或返回固定说明）
- smart：内部跑 ReAct 子图（agent→tools→agent 环）
节点只把结果写回外图状态（上下文隔离）。args 支持 "$<节点id>" 模板。
"""
from __future__ import annotations

import json
import time

from ..settings import as_settings
from .react import (build_react_subgraph, react_final_text, react_has_final,
                    react_trace)


class NodeExecutor:
    def __init__(self, provider, tools, settings=None, *, skill_tool_name: str = "load_skill",
                 tool_ctx=None):
        self.provider = provider
        self.tools = tools
        self.settings = as_settings(settings)
        self.skill_tool_name = skill_tool_name
        self.tool_ctx = tool_ctx
        # 由上层注入：系统提示与最近会话
        self.sys_text = ""
        self.history = []

    def _resolve_args(self, args: dict, state: dict) -> dict:
        results = state.get("results", {})
        out = {}
        for k, v in (args or {}).items():
            if isinstance(v, str) and v.startswith("$"):
                out[k] = str(results.get(v[1:], ""))
            else:
                out[k] = v
        return out

    async def run(self, node: dict, state: dict, on_event=None) -> dict:
        deps_text = "\n".join(
            f"[{d}] {state.get('results', {}).get(d, '')}" for d in node["deps"])
        if on_event:
            await on_event({"type": "node_start", "id": node["id"],
                            "title": node.get("title"), "ntype": node["type"],
                            "tool": node.get("tool")})
        t0 = time.time()
        if node["type"] == "deterministic":
            res = await self._run_deterministic(node, state, deps_text)
        else:
            res = await self._run_smart(node, state, deps_text, on_event)
        dur = round(time.time() - t0, 3)
        res["dur"] = dur
        for ev in res.get("events", []):
            if ev.get("type") == "node_done":
                ev["dur"] = dur
        return res

    async def _run_deterministic(self, node, state, deps_text):
        events = []
        args = self._resolve_args(node["args"], state)
        inp = (f"[确定性节点] 工具 {node['tool']}\n"
               f"[参数] {json.dumps(args, ensure_ascii=False)}")
        if deps_text:
            inp = f"[前置节点结果]\n{deps_text}\n\n" + inp
        res = await self.tools.execute(node["tool"], args, ctx=self.tool_ctx)
        if res.get("ok"):
            output, ok = str(res.get("result")), True
        elif res.get("needs_approval"):
            output = f"[待审批] {res.get('error')} / 单号 {res.get('approval_id')}"
            ok = False
        else:
            output = f"[失败] {res.get('error')}"
            ok = False
        events.append({"type": "node_done", "id": node["id"], "ok": ok,
                       "tool": node["tool"], "args": args,
                       "input": inp[:4000], "output": output[:4000]})
        return {"output": output, "ok": ok, "kind": "deterministic",
                "input": inp, "events": events}

    async def _run_smart(self, node, state, deps_text, on_event=None):
        events = []
        goal = node["goal"] or node["title"]
        user = f"节点目标：{goal}"
        if deps_text:
            user += f"\n\n前置节点结果：\n{deps_text}"
        sys_cap = self.settings.get_int("context.inject_sys_max_chars", 6000)
        extra = (getattr(self, "sys_text", "") or "")[:sys_cap]
        hist = getattr(self, "history", None) or []
        n = self.settings.get_int("orchestrator.react_ctx_msgs", 6)
        if hist and n > 0:
            cap = self.settings.get_int("orchestrator.react_ctx_chars", 300)
            lines = [f"{m.get('role')}：{(m.get('content') or '')[:cap]}"
                     for m in hist[-n:]]
            extra += "\n\n## 会话最近上下文（供理解指代，本次目标以下面为准）\n" + \
                     "\n".join(lines)
        # 已加载技能正文 → 对后续所有智能节点可见
        try:
            plan = state.get("plan") or {}
            get_res = state.get("results", {}) or {}
            skill_ids = [n["id"] for n in plan.get("nodes", [])
                         if n.get("tool") == self.skill_tool_name]
            loaded = "\n\n".join(str(get_res.get(i) or "")
                                 for i in skill_ids if get_res.get(i))
            if loaded:
                extra += ("\n\n## 已加载的技能（其中的铁律/禁止项是硬约束，必须逐条遵守）\n"
                          + loaded[:sys_cap])
        except Exception:  # noqa: BLE001 —— 拼技能失败不能拖垮节点
            pass
        reached = {"round": 0}

        async def on_step(info):
            reached["round"] = max(reached["round"], info.get("round") or 0)
            if on_event:
                await on_event({"type": "react_step", "id": node["id"],
                                "round": info.get("round"), "max": info.get("max"),
                                "tools": info.get("tools") or []})

        sub = build_react_subgraph(
            self.provider, self.tools, extra_system=extra,
            max_steps=self.settings.get_int("orchestrator.react_max_steps", 6),
            on_step=on_step, tool_ctx=self.tool_ctx)
        final_state = await sub.ainvoke({"messages": [{"role": "user", "content": user}],
                                         "durations": [], "steps": 0})
        output = react_final_text(final_state["messages"])
        _pend = final_state.get("pending") or {}
        if _pend:
            output = (f"⏳ 已提交审核，等待审核（工具 {_pend.get('tool')}，"
                      f"单号 {_pend.get('approval_id')}）")
            trace = react_trace(final_state["messages"], final_state.get("durations"))
            events.append({"type": "node_done", "id": node["id"], "ok": True,
                           "kind": "smart", "input": user[:4000],
                           "react_steps": trace, "output": output[:4000],
                           "rounds": reached["round"]})
            return {"output": output, "ok": True, "kind": "smart", "input": user,
                    "react": trace, "events": events}
        ok = react_has_final(final_state["messages"])
        if not ok:
            output = (f"[失败] 智能节点未给出最终答复（工具轮次用尽或模型未收尾）："
                      f"{output[:300] or '(无内容)'}")
        trace = react_trace(final_state["messages"], final_state.get("durations"))
        events.append({"type": "node_done", "id": node["id"], "ok": ok,
                       "kind": "smart", "input": user[:4000],
                       "react_steps": trace, "output": output[:4000],
                       "rounds": reached["round"]})
        return {"output": output, "ok": ok, "kind": "smart", "input": user,
                "react": trace, "events": events}
