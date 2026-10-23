# modules: agent_core.harness agent_core.api agent_core.context
"""端到端：SQLite / Markdown / PostgreSQL 三套后端跑同一组端口与 Agent 流程。

用法：
    python tests/e2e_harness_storage.py
    HARNESS_PG_DSN=postgresql://... python tests/e2e_harness_storage.py --backend postgres

没有 DSN 时 PostgreSQL 自动跳过；其它后端失败即退出非 0。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import uuid

from agent_core.adapters.memory import FakeModel, InMemoryStore
from agent_core.api import Agent, AgentConfig
from agent_core.context import MemoryRecord, assemble
from agent_core.domain import Decision, ModelResponse, RunRequest, Scope, ToolCall
from agent_core.harness import (MemoryContextPort, SQLiteApprovalQueue,
                                build_local_ports)
from agent_core.tooling import ToolRegistry


class AskPolicy:
    def check(self, ctx, action):
        return Decision("ask", "需要审批", "ap_e2e")


def _assert(cond, message):
    if not cond:
        raise AssertionError(message)


def run_backend(backend: str, dsn: str = "") -> None:
    tenant = f"e2e_{backend}_{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as tmp:
        ports = build_local_ports(tmp, backend=backend, dsn=dsn)
        try:
            # 1) 记忆：append / noop / supersede / 查询
            rec = MemoryRecord(tenant_id=tenant, kind="semantic", scope="project",
                               scope_id="p1", key="项目库", content="库=old",
                               source="user")
            r1 = ports.memory.upsert(rec)
            _assert(r1.action == "append", f"{backend}: memory append {r1}")
            r2 = ports.memory.upsert(MemoryRecord(
                tenant_id=tenant, kind="semantic", scope="project", scope_id="p1",
                key="项目库", content="库=old", source="agent"))
            _assert(r2.action == "noop", f"{backend}: memory noop {r2}")
            r3 = ports.memory.upsert(MemoryRecord(
                tenant_id=tenant, kind="semantic", scope="project", scope_id="p1",
                key="项目库", content="库=new", source="user"))
            _assert(r3.action == "supersede", f"{backend}: memory supersede {r3}")
            rows = ports.memory.query({"tenant_id": tenant, "scope": "project"})
            _assert([x.content for x in rows] == ["库=new"],
                    f"{backend}: memory query {[x.content for x in rows]}")

            # 2) 记忆 → 上下文闭环
            ctx = MemoryContextPort(ports.memory, tenant_id=tenant)
            out = assemble(context=ctx, identity={"tenant_id": tenant})
            _assert("库=new" in out["text"], f"{backend}: assemble {out['text']!r}")

            # 3) 知识：pending → approve → search → get → archive
            cid = ports.knowledge.add({
                "tenant_id": tenant, "scope": "project", "scope_id": "p1",
                "title": "报销制度", "source": "handbook.md",
                "content": "报销需要审批"})
            _assert(ports.knowledge.search("报销", tenant_id=tenant) == [],
                    f"{backend}: knowledge pending should be hidden")
            _assert(ports.knowledge.approve(cid, reviewer="e2e"),
                    f"{backend}: knowledge approve")
            hits = ports.knowledge.search("报销", tenant_id=tenant)
            _assert(hits and hits[0]["id"] == cid,
                    f"{backend}: knowledge search {hits}")
            _assert(ports.knowledge.get_chunk(cid, tenant_id=tenant),
                    f"{backend}: knowledge get")
            _assert(ports.knowledge.archive(cid), f"{backend}: knowledge archive")
            _assert(ports.knowledge.search("报销", tenant_id=tenant) == [],
                    f"{backend}: archived knowledge visible")

            # 4) 状态：save / checkpoint / resume / list
            thread = f"th_{uuid.uuid4().hex[:8]}"
            ports.state.save(thread, {"step": 1})
            _assert(ports.state.load(thread) == {"step": 1},
                    f"{backend}: state load")
            ports.state.checkpoint(thread, "plan", {"step": 2})
            ports.state.checkpoint(thread, "act", {"step": 3})
            _assert(ports.state.resume(thread) == {"step": 3},
                    f"{backend}: state resume")
            _assert([x["node"] for x in ports.state.list_checkpoints(thread)]
                    == ["plan", "act"], f"{backend}: state checkpoints")

            # 5) 审计：写入 / 查询 / 哈希链
            ports.audit.emit({"tenant_id": tenant, "actor": "u1",
                              "action": "e2e", "target": "test",
                              "decision": "ok"})
            audit_rows = ports.audit.query({"tenant_id": tenant})
            _assert(len(audit_rows) >= 1, f"{backend}: audit query")
            _assert(ports.audit.verify_chain()["ok"],
                    f"{backend}: audit chain")

            # 6) Agent：真实运行 + 检查点 + 审计
            model = FakeModel([ModelResponse(content="完成")])
            agent = Agent(AgentConfig(
                scope=Scope(tenant_id=tenant, agent_id="a1", user_id="u1"),
                model=model, store=InMemoryStore(), tools=ToolRegistry(),
                audit=ports.audit, state_port=ports.state))
            res = agent.run(RunRequest(objective="打个招呼"))
            _assert(res.status == "ok", f"{backend}: agent {res.status}")
            nodes = [x["node"] for x in ports.state.list_checkpoints(res.run_id)]
            _assert(nodes == ["start", "done"], f"{backend}: agent checkpoints {nodes}")
            _assert(any(e.action == "run:done"
                        for e in ports.audit.query({"tenant_id": tenant})),
                    f"{backend}: agent audit")
        finally:
            ports.close()


def run_approval_e2e() -> None:
    """审批闭环：blocked → approve → 工具执行（与存储后端无关）。"""
    seen = []
    tools = ToolRegistry()
    tools.register("calc", lambda expr="": (seen.append(expr) or "2"), "计算")
    queue = SQLiteApprovalQueue()
    with tempfile.TemporaryDirectory() as tmp:
        ports = build_local_ports(tmp, backend="sqlite")
        model = FakeModel([
            ModelResponse(content="", tool_calls=(ToolCall("c1", "calc",
                                                          {"expr": "1+1"}),)),
            ModelResponse(content="结果是 2"),
        ])
        agent = Agent(AgentConfig(
            scope=Scope(tenant_id="t1", agent_id="a1", user_id="u1"),
            model=model, store=InMemoryStore(), tools=tools,
            policy=AskPolicy(), approval_queue=queue,
            audit=ports.audit, state_port=ports.state))
        first = agent.run(RunRequest(objective="算 1+1"))
        _assert(first.status == "blocked", f"approval: first {first.status}")
        data = dict(first.pending.data)
        model2 = FakeModel([
            ModelResponse(content="", tool_calls=(ToolCall("c1", "calc",
                                                          {"expr": "1+1"}),)),
            ModelResponse(content="结果是 2"),
        ])
        agent2 = Agent(AgentConfig(
            scope=Scope(tenant_id="t1", agent_id="a1", user_id="u1"),
            model=model2, store=InMemoryStore(), tools=tools,
            policy=AskPolicy(), approval_queue=queue,
            audit=ports.audit, state_port=ports.state))
        second = agent2.run(RunRequest(
            objective="算 1+1",
            metadata={"approval_decision": {**data, "decision": "approve"}}))
        _assert(second.status == "ok", f"approval: second {second.status}")
        _assert(seen == ["1+1"], f"approval: tool not executed {seen}")
        ports.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", action="append", default=[],
                        choices=["sqlite", "markdown", "postgres"])
    parser.add_argument("--dsn", default=os.environ.get("HARNESS_PG_DSN", ""))
    args = parser.parse_args()
    backends = args.backend or ["sqlite", "markdown", "postgres"]
    ran = []
    for backend in backends:
        if backend == "postgres" and not args.dsn:
            print("SKIP postgres：未提供 HARNESS_PG_DSN")
            continue
        t0 = time.time()
        run_backend(backend, args.dsn if backend == "postgres" else "")
        ran.append(backend)
        print(f"E2E_OK backend={backend} {time.time() - t0:.2f}s")
    run_approval_e2e()
    print("E2E_OK approval_loop")
    print("E2E_ALL_OK", ",".join(ran) or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
