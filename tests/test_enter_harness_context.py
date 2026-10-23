"""上下文：窗口预算、自动压缩、去重、记忆注入；提示词分层注册表。"""
import unittest

from enter_harness import ContextManager, DictSettings, PromptRegistry, SessionManager
from enter_harness.prompts.registry import (LAYER_CORE, LAYER_RUNTIME,
                                         LAYER_TASK)

from core_fakes import MemoryStore, ScriptedProvider


class Memory:
    def __init__(self, text=""):
        self.text = text
        self.calls = []

    def inject(self, query, token_budget=600):
        self.calls.append((query, token_budget))
        return {"text": self.text, "used_chars": len(self.text),
                "count": 1 if self.text else 0}


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.sessions = SessionManager(self.store)
        self.sid = self.sessions.create("t")["id"]

    def _ctx(self, **cfg):
        return ContextManager(self.sessions, settings=DictSettings(cfg))

    async def test_window_keeps_recent(self):
        for i in range(10):
            self.sessions.add(self.sid, "user", f"m{i}")
        ctx = self._ctx(**{"context.window_keep": 3,
                           "context.window_max_chars": 1000})
        w = ctx.window(self.sid)
        self.assertEqual([m["content"] for m in w], ["m7", "m8", "m9"])

    async def test_compact_idempotent(self):
        for i in range(25):
            self.sessions.add(self.sid, "user" if i % 2 == 0 else "assistant",
                              f"message {i}")
        ctx = self._ctx(**{"context.window_keep": 5,
                           "context.compact_trigger": 20})
        p = ScriptedProvider(complete_text="摘要内容")
        r = await ctx.compact_if_needed(self.sid, p)
        self.assertTrue(r["compacted"])
        self.assertEqual(r["summary"], "摘要内容")
        self.assertEqual(p.complete_calls, 1)
        # 原文被标 compacted，上下文里只剩摘要 + 最近 5 条
        active = self.sessions.context_history(self.sid)
        self.assertEqual(len(active), 6)
        self.assertEqual(active[-1]["role"], "summary")
        # 再跑一次不重复压缩（未压缩消息已不够触发）
        r2 = await ctx.compact_if_needed(self.sid, p)
        self.assertFalse(r2["compacted"])

    async def test_compact_keep_zero(self):
        for i in range(5):
            self.sessions.add(self.sid, "user", f"m{i}")
        ctx = self._ctx(**{"context.window_keep": 0,
                           "context.compact_trigger": 1})
        r = await ctx.compact_if_needed(self.sid, ScriptedProvider())
        self.assertTrue(r["compacted"])

    async def test_build_dedupes_current_user_message(self):
        self.sessions.add(self.sid, "user", "你好")
        ctx = self._ctx()
        out = await ctx.build(self.sid, "你好", ScriptedProvider())
        users = [m for m in out["messages"] if m["role"] == "user"]
        self.assertEqual(len(users), 1)

    async def test_memory_injection(self):
        mem = Memory("用户偏好：先给结论")
        ctx = ContextManager(self.sessions,
                             settings=DictSettings({"context.memory_inject": True}),
                             memory=mem)
        out = await ctx.build(self.sid, "问题", ScriptedProvider())
        self.assertIn("先给结论", out["messages"][0]["content"])
        self.assertIn("相关记忆", out["messages"][0]["content"])
        self.assertEqual(mem.calls[0][0], "问题")

    async def test_memory_disabled(self):
        mem = Memory("x")
        ctx = ContextManager(self.sessions, settings=DictSettings(), memory=mem)
        out = await ctx.build(self.sid, "问题", ScriptedProvider())
        self.assertTrue(out["injected_memory"]["disabled"])
        self.assertEqual(mem.calls, [])


class PromptTests(unittest.TestCase):
    def test_layer_order(self):
        p = PromptRegistry()
        p.add("task", "T", layer=LAYER_TASK)
        p.add("core", "C", layer=LAYER_CORE)
        p.add("runtime", "R", layer=LAYER_RUNTIME)
        self.assertEqual(p.render(), "C\n\nR\n\nT")

    def test_override_and_missing(self):
        p = PromptRegistry().add("x", "old")
        p.override("x", "new")
        self.assertEqual(p.get("x"), "new")
        # 覆写不存在的层不报错
        p.override("ghost", "g")
        self.assertEqual(p.get("ghost"), "g")

    def test_dynamic_section(self):
        box = {"v": "a"}
        p = PromptRegistry().add("core", "C")
        p.section("dyn", lambda: box["v"], layer=LAYER_RUNTIME)
        self.assertIn("a", p.render())
        box["v"] = "b"
        self.assertIn("b", p.render())
        # 动态片段异常不拖垮渲染
        p.section("bad", lambda: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(p.render(), "C\n\nb")

    def test_remove_and_names(self):
        p = PromptRegistry().add("a", "A").add("b", "B")
        p.remove("a")
        self.assertEqual(p.names(), ["b"])


if __name__ == "__main__":
    unittest.main()
