"""Graph Engine：用 LangGraph 根据 Planner 的任务图动态构建并执行。

- 输入：plan（nodes + deps）
- 构建：为每个节点 add_node，按 deps 连边；无依赖从 START 起，无被依赖接 END
- 执行：super-step 模型；无依赖节点可并行
- Checkpointer：AsyncSqliteSaver 落盘 → 断点/恢复（同 thread_id 续跑）
"""
from __future__ import annotations

import operator
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph


def _merge_dict(a, b):
    return {**(a or {}), **(b or {})}


class GraphState(TypedDict):
    results: Annotated[dict, _merge_dict]
    events: Annotated[list, operator.add]
    plan: dict


class GraphEngine:
    def __init__(self, store=None, checkpoint_path: str | Path | None = None):
        self.store = store
        self.checkpoint_path = Path(
            checkpoint_path or os.environ.get("AGENT_CORE_CHECKPOINT", "checkpoints.db"))

    @asynccontextmanager
    async def _checkpointer(self):
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
            try:
                await saver.setup()
            except Exception:  # noqa: BLE001 —— 已建表时忽略
                pass
            yield saver

    def build(self, plan: dict, executor, checkpointer, on_event=None, reuse=None):
        g = StateGraph(GraphState)
        nodes = plan["nodes"]
        deps_ids = {d for n in nodes for d in n["deps"]}

        for n in nodes:
            g.add_node(f"n_{n['id']}",
                       self._make_node_fn(n, executor, on_event, reuse))

        for n in nodes:
            if n["deps"]:
                for d in n["deps"]:
                    g.add_edge(f"n_{d}", f"n_{n['id']}")
            else:
                g.add_edge(START, f"n_{n['id']}")
            if n["id"] not in deps_ids:
                g.add_edge(f"n_{n['id']}", END)
        return g.compile(checkpointer=checkpointer)

    @staticmethod
    def _make_node_fn(node, executor, on_event=None, reuse=None):
        async def fn(state: GraphState):
            if reuse and node["id"] in reuse:
                out = reuse[node["id"]]
                if on_event:
                    await on_event({"type": "node_done", "id": node["id"], "ok": True,
                                    "reused": True, "kind": node.get("type"),
                                    "tool": node.get("tool"),
                                    "input": "（复用上一轮结果）",
                                    "output": str(out)[:4000]})
                return {"results": {node["id"]: out}, "events": []}
            res = await executor.run(node, state, on_event=on_event)
            return {"results": {node["id"]: res["output"]},
                    "events": res.get("events", [])}
        fn.__name__ = f"node_{node['id']}"
        return fn

    async def run(self, plan: dict, executor, thread_id: str, session_id: str = "",
                  on_event=None, reuse=None) -> dict:
        async with self._checkpointer() as cp:
            graph = self.build(plan, executor, cp, on_event=on_event, reuse=reuse)
            cfg = {"configurable": {"thread_id": thread_id}}
            async for chunk in graph.astream(
                    {"results": {}, "events": [], "plan": plan}, cfg,
                    stream_mode="updates"):
                for _node, update in (chunk or {}).items():
                    for ev in (update or {}).get("events", []) or []:
                        if on_event:
                            await on_event(ev)
            snapshot = await graph.aget_state(cfg)
            values = snapshot.values if snapshot else {}
            result = {
                "results": values.get("results", {}),
                "events": values.get("events", []),
                "ok": all(not str(v).startswith("[失败]")
                          for v in values.get("results", {}).values()),
            }
            if self.store is not None and hasattr(self.store, "save_snapshot"):
                self.store.save_snapshot(thread_id, session_id, plan, {
                    "results": result["results"], "node_count": len(plan["nodes"])})
            return result

    async def resume_state(self, plan: dict, executor, thread_id: str) -> dict:
        async with self._checkpointer() as cp:
            graph = self.build(plan, executor, cp)
            cfg = {"configurable": {"thread_id": thread_id}}
            snap = await graph.aget_state(cfg)
            return {"values": snap.values if snap else {},
                    "next": list(snap.next) if snap else []}
