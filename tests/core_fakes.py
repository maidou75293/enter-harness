"""enter_harness 测试替身：内存 Store / 脚本化 Provider / 工具辅助。

测试只依赖 enter_harness，不 import app。
"""
import asyncio
import json
import time


class MemoryStore:
    """实现 core 用到的 Store 协议子集。"""

    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.snapshots = []
        self._mid = 0

    def create_session(self, sid, title="", tenant_id="default"):
        now = time.time()
        row = {"id": sid, "title": title, "tenant_id": tenant_id,
               "created": now, "updated": now}
        self.sessions[sid] = row
        return dict(row)

    def list_sessions(self, tenant_id=None):
        return [dict(v) for v in self.sessions.values()
                if tenant_id is None or v["tenant_id"] == tenant_id]

    def get_session(self, sid):
        v = self.sessions.get(sid)
        return dict(v) if v else None

    def delete_session(self, sid):
        self.sessions.pop(sid, None)
        self.messages.pop(sid, None)

    def add_message(self, sid, role, content, meta=None):
        self._mid += 1
        self.messages.setdefault(sid, []).append({
            "id": self._mid, "session_id": sid, "role": role,
            "content": content, "meta": dict(meta or {}),
            "compacted": 0, "created": time.time()})
        return self._mid

    def list_messages(self, sid, limit=None):
        rows = [dict(x) for x in self.messages.get(sid, [])]
        return rows[-limit:] if limit else rows

    def list_active_messages(self, sid, limit=None):
        rows = [dict(x) for x in self.messages.get(sid, [])
                if not x.get("compacted")]
        return rows[-limit:] if limit else rows

    def mark_compacted(self, ids):
        wanted = set(ids)
        for rows in self.messages.values():
            for m in rows:
                if m["id"] in wanted:
                    m["compacted"] = 1

    def save_snapshot(self, thread_id, session_id, plan, state):
        self.snapshots.append({"thread_id": thread_id, "session_id": session_id,
                               "plan": plan, "state": state})

    def append_run_events_batch(self, run_id, session_id, start_seq, events):
        return start_seq + len(events) - 1


class ScriptedProvider:
    """按脚本返回 chat 响应；记录每次调用（消息/tools）。"""

    def __init__(self, responses=None, final="完成", complete_text="摘要",
                 delay=0.0):
        self.responses = list(responses or [])
        self.final = final
        self.complete_text = complete_text
        self.delay = delay
        self.calls = []
        self.complete_calls = 0

    async def chat(self, messages, tools=None, tool_choice=None,
                   response_format=None, temperature=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append({"messages": [dict(m) for m in messages],
                           "tools": tools})
        if self.responses:
            r = self.responses.pop(0)
            return r() if callable(r) else r
        return {"role": "assistant", "content": self.final}

    async def complete(self, messages, kind="chat"):
        self.complete_calls += 1
        return self.complete_text


def call(cid, name, args, raw=None):
    return {"id": cid, "type": "function", "function": {
        "name": name,
        "arguments": raw if raw is not None else json.dumps(args, ensure_ascii=False)}}


def assistant_tools(*calls, content=""):
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


def final(text="完成"):
    return {"role": "assistant", "content": text}


class RecordingBus:
    """最小工具总线替身：记录并发度与调用顺序。"""

    def __init__(self, parallel=False, fail=None, sleep=0.0):
        self.parallel = parallel
        self.fail = set(fail or [])
        self.sleep = sleep
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.specs = []

    def openai_tools(self, names=None):
        return self.specs

    def is_parallel_safe(self, name):
        return self.parallel

    async def execute(self, name, args, ctx=None):
        self.calls.append((name, args))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.sleep:
                await asyncio.sleep(self.sleep)
            if name in self.fail:
                return {"ok": False, "error": "boom"}
            return {"ok": True, "result": f"{name}:ok"}
        finally:
            self.active -= 1
