"""Planner：让模型输出任务图（节点 + 依赖），用原生 tool calling 提交。

输出契约：
{
  "goal": str,
  "nodes": [
     {"id","title","type":"deterministic|smart","deps":[...],
      "tool": str?, "args": {...}?,   # deterministic
      "goal": str?}                    # smart
  ],
  "final_node": str
}
"""
from __future__ import annotations

from ..llm.openai_compat import tool_result

PLAN_TOOL = {"type": "function", "function": {
    "name": "submit_plan",
    "description": "提交任务图（节点 + 依赖 + 最终节点）",
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "任务总目标（忠实于用户需求）"},
            "nodes": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "type": {"type": "string", "enum": ["deterministic", "smart"]},
                    "deps": {"type": "array", "items": {"type": "string"}},
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                    "goal": {"type": "string"},
                },
                "required": ["id", "title", "type"],
            }},
            "final_node": {"type": "string"},
        },
        "required": ["goal", "nodes", "final_node"],
    },
}}

DEFAULT_PLANNER_SYSTEM = """你是任务规划器。把用户需求拆成一张任务图，用 `submit_plan` 工具提交。
节点类型：
- deterministic：能确定执行的节点，需给出 tool 与 args。
- smart：需要推理/多步工具使用的节点，给出 goal（内部走 ReAct）。
要求：
- 用 deps 表示依赖，必须给出 final_node。
- goal 忠实于用户需求，不得改写/降低，不得加“做不到就给步骤/建议”的退路。
- 任务图必须真正执行需求；最终节点输出必须是需求已达成的证据。
- 可用工具见下方清单。"""


class Planner:
    def __init__(self, provider, settings, tools, system_prompt: str | None = None,
                 plan_tool: dict | None = None,
                 extra_node_types: tuple = ()):
        self.provider = provider
        self.settings = settings
        self.tools = tools
        self.system_prompt = system_prompt or DEFAULT_PLANNER_SYSTEM
        self.plan_tool = plan_tool or PLAN_TOOL
        # 宿主扩展节点类型（如 agent / team）；内核只校验内置两类
        self.extra_node_types = tuple(extra_node_types or ())

    def _tool_listing(self) -> str:
        return "\n".join(f"- {t['name']}：{t['description']}"
                         for t in self.tools.list_tools())

    def _plan_prompt(self, requirement: str, hint: str = "", context=None) -> list:
        system = self.system_prompt + "\n可用工具：\n" + self._tool_listing()
        msgs = [{"role": "system", "content": system}]
        if context:
            head = context[0] if context and context[0].get("role") == "system" else None
            if head and (head.get("content") or "").strip():
                msgs[0]["content"] = system + "\n\n" + head["content"]
            for m in context[1:-1]:
                if m.get("role") in ("user", "assistant"):
                    msgs.append({"role": m["role"], "content": m.get("content", "")})
        msgs.append({"role": "user", "content":
                     (f"（上下文提示：{hint}）\n" if hint else "") +
                     f"用户需求：{requirement}\n请提交任务图。"})
        return msgs

    async def plan(self, requirement: str, hint: str = "", context=None) -> dict:
        msgs = self._plan_prompt(requirement, hint, context)
        msg = await self.provider.chat(
            msgs, tools=[self.plan_tool],
            tool_choice={"type": "function",
                         "function": {"name": self.plan_tool["function"]["name"]}})
        return self.validate(tool_result(msg, self.plan_tool["function"]["name"]), requirement)

    async def replan(self, requirement: str, prev_plan: dict, feedback: str,
                     context=None) -> dict:
        msgs = self._plan_prompt(requirement, "", context) + [
            {"role": "assistant", "content": "上一版任务图：" + str(prev_plan)},
            {"role": "user", "content": f"校验反馈：{feedback}\n请提交**修订后**的任务图。"},
        ]
        msg = await self.provider.chat(
            msgs, tools=[self.plan_tool],
            tool_choice={"type": "function",
                         "function": {"name": self.plan_tool["function"]["name"]}})
        return self.validate(tool_result(msg, self.plan_tool["function"]["name"]), requirement)

    # ---------------- 校验 / 规整 ----------------
    def validate(self, plan: dict, requirement: str = "") -> dict:
        if not plan or not plan.get("nodes"):
            plan = {"goal": requirement, "nodes": [
                {"id": "n1", "title": "直接回答", "type": "smart",
                 "goal": requirement, "deps": []}], "final_node": "n1"}

        nodes, seen = [], set()
        for i, n in enumerate(plan.get("nodes", [])):
            nid = str(n.get("id") or f"n{i + 1}")
            if nid in seen:
                nid = f"{nid}_{i}"
            seen.add(nid)
            allowed_types = ("deterministic", "smart") + self.extra_node_types
            ntype = n.get("type") if n.get("type") in allowed_types else "smart"
            nodes.append({
                "id": nid,
                "title": n.get("title") or nid,
                "type": ntype,
                "deps": [str(d) for d in (n.get("deps") or [])],
                "tool": n.get("tool"),
                "args": n.get("args") or {},
                "goal": n.get("goal") or n.get("title") or "",
            })

        ids = {n["id"] for n in nodes}
        for n in nodes:
            n["deps"] = [d for d in n["deps"] if d in ids and d != n["id"]]
        nodes = self._break_cycles(nodes)

        for n in nodes:
            if n["type"] == "deterministic" and (not n["tool"] or not self.tools.has(n["tool"])):
                n["type"] = "smart"
                n["goal"] = n["goal"] or f"完成：{n['title']}"

        final = str(plan.get("final_node") or (self._sinks(nodes)[0] if nodes else ""))
        if final not in ids:
            sinks = self._sinks(nodes)
            final = sinks[0] if sinks else nodes[-1]["id"]
        return {"goal": plan.get("goal", requirement), "nodes": nodes, "final_node": final}

    @staticmethod
    def _break_cycles(nodes: list) -> list:
        remaining = {n["id"]: n for n in nodes}
        resolved = set()
        changed = True
        while changed:
            changed = False
            for nid, n in list(remaining.items()):
                if all(d in resolved for d in n["deps"]):
                    resolved.add(nid)
                    del remaining[nid]
                    changed = True
        for n in remaining.values():
            n["deps"] = [d for d in n["deps"] if d not in remaining]
        return nodes

    @staticmethod
    def _sinks(nodes: list) -> list:
        used = {d for n in nodes for d in n["deps"]}
        return [n["id"] for n in nodes if n["id"] not in used]
