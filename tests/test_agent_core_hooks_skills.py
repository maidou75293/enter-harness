"""Hooks 生命周期与 Skills（SKILL.md 扫描/清单/加载/自建/私有优先）。"""
import tempfile
import unittest
from pathlib import Path

from enter_harness import HookDenied, HookManager, SkillLibrary


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def test_priority_order(self):
        h = HookManager()
        seen = []
        h.register("run_start", lambda **kw: seen.append("low"), priority=10)
        h.register("run_start", lambda **kw: seen.append("high"), priority=-1)
        await h.emit("run_start")
        self.assertEqual(seen, ["high", "low"])

    async def test_before_tool_chain(self):
        h = HookManager()

        @h.on("before_tool")
        async def first(tool, args, ctx=None):
            return {"args": {**args, "x": args.get("x", 0) + 1}}

        @h.on("before_tool", priority=10)
        def second(tool, args, ctx=None):
            return {"args": {**args, "x": args["x"] * 10}}

        out = await h.before_tool("f", {"x": 1})
        self.assertEqual(out, {"x": 20})

    async def test_before_tool_deny(self):
        h = HookManager()
        h.register("before_tool", lambda **kw: {"deny": "危险"})
        with self.assertRaises(HookDenied) as cm:
            await h.before_tool("rm", {})
        self.assertEqual(cm.exception.reason, "危险")

    async def test_after_tool_merge(self):
        h = HookManager()
        h.register("after_tool", lambda **kw: {"result": "改写", "extra": 1})
        out = await h.after_tool("f", {}, {"ok": True, "result": "原值"})
        self.assertEqual(out["result"], "改写")
        self.assertEqual(out["extra"], 1)
        self.assertTrue(out["ok"])

    async def test_hook_failure_isolated_by_default(self):
        h = HookManager()
        h.register("run_start", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertEqual(await h.emit("run_start"), [])

    async def test_strict_raises(self):
        h = HookManager(strict=True)
        h.register("run_start", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
        with self.assertRaises(RuntimeError):
            await h.emit("run_start")

    async def test_off_and_clear(self):
        h = HookManager()
        fn = h.register("run_start", lambda **kw: 1)
        self.assertEqual(len(h.list("run_start")), 1)
        h.off("run_start", fn)
        self.assertEqual(h.list("run_start"), [])
        h.register("a", lambda **kw: 1)
        h.register("b", lambda **kw: 2)
        h.clear("a")
        self.assertEqual(h.list("a"), [])
        self.assertEqual(len(h.list("b")), 1)


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="enter-harness-skills-"))
        self.global_dir = self.tmp / "global"
        self.private = self.tmp / "private"
        for base, name, desc in ((self.global_dir, "deploy", "部署流程"),
                                 (self.global_dir, "review", "代码评审"),
                                 (self.private, "deploy", "私有部署")):
            d = base / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {desc}\n---\n\n正文 {desc}\n",
                encoding="utf-8")
        self.lib = SkillLibrary(self.global_dir, private_dir=lambda: self.private)

    def test_list_and_manifest(self):
        names = {x["name"] for x in self.lib.list()}
        self.assertEqual(names, {"deploy", "review"})
        m = self.lib.manifest()
        self.assertIn("deploy", m)
        self.assertIn("review", m)

    def test_private_precedence(self):
        self.assertIn("私有部署", self.lib.load("deploy"))

    def test_load_missing(self):
        self.assertIn("未找到", self.lib.load("nope"))

    def test_create_and_duplicate(self):
        r = self.lib.create("mine", "我的技能", "正文")
        self.assertTrue(r["ok"])
        self.assertEqual(self.lib.load("mine"), "正文")
        self.assertIn("我的技能",
                      next(x["description"] for x in self.lib.list()
                           if x["name"] == "mine"))
        r2 = self.lib.create("mine")
        self.assertFalse(r2["ok"])
        self.assertIn("已存在", r2["error"])

    def test_invalid_name(self):
        for bad in ("", "../x", "a/b", "a\\b"):
            self.assertFalse(self.lib.create(bad)["ok"])

    def test_tool_adapters(self):
        self.assertIn("已创建技能", self.lib.tool_create("mine2", "我的技能", "正文"))
        self.assertEqual(self.lib.tool_load("mine2"), "正文")


if __name__ == "__main__":
    unittest.main()
