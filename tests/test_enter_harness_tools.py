"""工具系统与权限：schema、白名单、策略、hooks、审批、失败、输出治理。"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from enter_harness import HookDenied, HookManager, PolicyDecision, RulePolicy
from enter_harness.tools import ToolRegistry
from enter_harness.contracts import ToolSpec
from enter_harness.tools import OutputLimiter, land_output, safe_path


class PolicyTests(unittest.TestCase):
    def test_priority_deny_ask_allow(self):
        p = RulePolicy(deny=["x*"], ask=["*"], allow=["*"])
        self.assertEqual(p.check({"name": "x1"}).decision, "deny")
        self.assertEqual(p.check({"name": "y1"}).decision, "ask")

    def test_danger_default_ask(self):
        p = RulePolicy(danger_decision="ask")
        self.assertEqual(p.check({"name": "rm", "danger": True}).decision, "ask")
        self.assertEqual(p.check({"name": "read"}).decision, "allow")

    def test_disabled_and_allowed_tools(self):
        self.assertEqual(RulePolicy(enabled=False).check({"name": "x"}).decision, "deny")
        d = RulePolicy().check({"name": "x"}, {"allowed_tools": ["y"]})
        self.assertEqual(d.decision, "deny")

    def test_default_deny(self):
        self.assertEqual(RulePolicy(default="deny").check({"name": "x"}).decision,
                         "deny")


class Approver:
    def __init__(self, ok):
        self.ok = ok
        self.seen = []

    async def approve(self, tool_meta, ctx, decision):
        self.seen.append((tool_meta["name"], decision.reason))
        return self.ok


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="enter-harness-tools-"))

    async def test_schema_and_whitelist(self):
        reg = ToolRegistry()
        reg.register("echo", lambda text="": text, "回显",
                     {"type": "object", "properties": {"text": {"type": "string"}}},
                     parallel=True)
        self.assertEqual(reg.openai_tools()[0]["function"]["name"], "echo")
        self.assertTrue(reg.is_parallel_safe("echo"))
        reg.set_whitelist(["echo", "other"])
        r = await reg.execute("missing", {})
        self.assertFalse(r["ok"])

    async def test_sync_and_async_handlers(self):
        reg = ToolRegistry()

        def sync(x=1):
            return x + 1

        async def async_(x=1):
            return x + 2

        reg.register("s", sync)
        reg.register("a", async_)
        self.assertEqual((await reg.execute("s", {"x": 1}))["result"], 2)
        self.assertEqual((await reg.execute("a", {"x": 1}))["result"], 3)

    async def test_exception_is_recoverable(self):
        reg = ToolRegistry()
        reg.register("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        r = await reg.execute("boom", {})
        self.assertFalse(r["ok"])
        self.assertIn("RuntimeError", r["error"])

    async def test_before_tool_can_rewrite_and_deny(self):
        hooks = HookManager()
        hooks.register("before_tool", lambda tool, args, ctx=None: {"args": {"x": 9}})
        reg = ToolRegistry(hooks=hooks)
        reg.register("f", lambda x=0: x)
        self.assertEqual((await reg.execute("f", {"x": 1}))["result"], 9)

        hooks2 = HookManager()
        hooks2.register("before_tool", lambda **kw: {"deny": "禁止"})
        reg2 = ToolRegistry(hooks=hooks2)
        reg2.register("f", lambda: "x")
        r = await reg2.execute("f", {})
        self.assertFalse(r["ok"])
        self.assertTrue(r["denied"])
        self.assertEqual(r["error"], "禁止")

    async def test_after_tool_can_override_result(self):
        hooks = HookManager()
        hooks.register("after_tool", lambda tool, args, result, ctx=None: {"result": "改写"})
        reg = ToolRegistry(hooks=hooks)
        reg.register("f", lambda: "原值")
        self.assertEqual((await reg.execute("f", {}))["result"], "改写")

    async def test_policy_ask_without_approver(self):
        reg = ToolRegistry(RulePolicy(ask=["*"]))
        reg.register("f", lambda: "x")
        r = await reg.execute("f", {})
        self.assertTrue(r["needs_approval"])
        self.assertFalse(r["ok"])
        self.assertEqual(reg.pop_pending_approval()["tool"], "f")

    async def test_policy_ask_with_approver(self):
        ap = Approver(True)
        reg = ToolRegistry(RulePolicy(ask=["*"]), approver=ap)
        reg.register("f", lambda: "x")
        r = await reg.execute("f", {})
        self.assertTrue(r["ok"])
        self.assertEqual(ap.seen[0][0], "f")

    async def test_policy_deny(self):
        reg = ToolRegistry(RulePolicy(deny=["f"]))
        reg.register("f", lambda: "x")
        r = await reg.execute("f", {})
        self.assertFalse(r["ok"])
        self.assertTrue(r["denied"])

    async def test_strict_schema(self):
        reg = ToolRegistry(strict_schema=True)
        reg.register("f", lambda **kw: "x", parameters={
            "type": "object", "properties": {"n": {"type": "integer"}},
            "required": ["n"]})
        r1 = await reg.execute("f", {})
        self.assertIn("缺少必填", r1["error"])
        r2 = await reg.execute("f", {"n": "x"})
        self.assertIn("integer", r2["error"])
        r3 = await reg.execute("f", {"n": 1})
        self.assertTrue(r3["ok"])

    async def test_disabled_and_subset(self):
        reg = ToolRegistry()
        reg.register("a", lambda: 1)
        reg.register("b", lambda: 2)
        sub = reg.subset(["a"])
        self.assertTrue((await sub.execute("a", {}))["ok"])
        r = await sub.execute("b", {})
        self.assertFalse(r["ok"])
        self.assertTrue(r["denied"])
        reg.register("off", lambda: 3, enabled=False)
        self.assertFalse(reg.has("off"))

    async def test_output_landing(self):
        reg = ToolRegistry(output_limiter=OutputLimiter(
            max_lines=5, max_bytes=100, keep_lines=2, keep_bytes=40),
            output_root=lambda: self.tmp)
        reg.register("big", lambda: "\n".join(f"line{i}" for i in range(100)))
        r = await reg.execute("big", {})
        self.assertIn("完整输出已落盘", r["result"])
        files = list((self.tmp / ".outputs").glob("*.txt"))
        self.assertEqual(len(files), 1)
        self.assertIn("line99", files[0].read_text(encoding="utf-8"))


class WorkspaceTests(unittest.TestCase):
    def test_safe_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="enter-harness-ws-"))
        p = safe_path("a/b.txt", tmp)
        # 跨平台断言：Windows 的 str(path) 用反斜杠，不能按 "a/b.txt" 判断
        self.assertEqual(p.parts[-2:], ("a", "b.txt"))
        self.assertTrue(p.is_relative_to(Path(tmp).resolve()))
        with self.assertRaises(ValueError):
            safe_path("../escape.txt", tmp)

    def test_land_output_non_string(self):
        self.assertEqual(land_output("x", {"a": 1}), {"a": 1})


if __name__ == "__main__":
    unittest.main()
