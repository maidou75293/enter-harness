"""子代理：隔离上下文、工具白名单、嵌套深度上限、trace。"""
import unittest

from agent_core import SubagentRunner
from agent_core.tools import ToolRegistry
from agent_core.errors import SubagentDepthExceeded
from agent_core.runtime import current_depth

from core_fakes import ScriptedProvider, assistant_tools, call, final


def _registry():
    reg = ToolRegistry()
    reg.register("read", lambda path="": f"内容:{path}", parallel=True)
    reg.register("write", lambda path="", content="": "已写")
    return reg


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_context_and_whitelist(self):
        p = ScriptedProvider([
            assistant_tools(call("c1", "read", {"path": "a"})),
            final("子结果"),
        ])
        runner = SubagentRunner(p, _registry(), max_steps=3)
        r = await runner.run("读 a", tools=["read"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["answer"], "子结果")
        # 子代理只看得到白名单工具
        names = [x["function"]["name"] for x in p.calls[0]["tools"]]
        self.assertEqual(names, ["read"])
        # 不继承父历史：第一条是子代理 system，第二条是任务
        self.assertEqual(p.calls[0]["messages"][0]["role"], "system")
        self.assertIn("子代理", p.calls[0]["messages"][0]["content"])
        self.assertEqual(p.calls[0]["messages"][1]["content"], "读 a")

    async def test_disallowed_tool_is_not_executable(self):
        p = ScriptedProvider([
            assistant_tools(call("c1", "write", {"path": "a", "content": "x"})),
            final("无法写入"),
        ])
        runner = SubagentRunner(p, _registry(), max_steps=3)
        r = await runner.run("写文件", tools=["read"])
        tool_msg = [m for m in p.calls[-1]["messages"]
                    if m.get("role") == "tool"][0]
        self.assertIn("未知工具", tool_msg["content"])
        self.assertEqual(r["answer"], "无法写入")

    async def test_depth_limit(self):
        runner = SubagentRunner(ScriptedProvider(), _registry(), max_depth=1)
        token = current_depth.set(1)
        try:
            with self.assertRaises(SubagentDepthExceeded):
                await runner.run("nested")
        finally:
            current_depth.reset(token)

    async def test_trace_returned(self):
        runner = SubagentRunner(ScriptedProvider(final="ok"), _registry())
        r = await runner.run("任务")
        self.assertIn("trace", r)
        self.assertIn("final", [e["type"] for e in r["trace"]])


if __name__ == "__main__":
    unittest.main()
