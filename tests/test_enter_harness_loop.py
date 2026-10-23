"""主循环：工具调用、失败恢复、坏参数、max_steps finalize、超时、中断、steering。"""
import asyncio
import unittest

from enter_harness.loop import AgentLoop
from enter_harness.loop.loop import _failure_hint, age_tool_outputs

from core_fakes import (RecordingBus, ScriptedProvider, assistant_tools,
                        call, final)


def _loop(provider, bus, **kw):
    kw.setdefault("max_steps", 5)
    kw.setdefault("timeout", 5)
    return AgentLoop(provider, bus, system_prompt="sys", **kw)


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_simple_final(self):
        p = ScriptedProvider(final="答案")
        r = await _loop(p, RecordingBus()).run("问题")
        self.assertTrue(r.ok)
        self.assertEqual(r.answer, "答案")
        self.assertEqual(r.stopped_reason, "final")
        self.assertEqual(r.steps, 1)
        self.assertEqual(p.calls[0]["messages"][0]["role"], "system")

    async def test_tool_call_then_final(self):
        p = ScriptedProvider([
            assistant_tools(call("c1", "calc", {"expr": "1+1"})),
            final("结果 2"),
        ])
        r = await _loop(p, RecordingBus()).run("算一下")
        self.assertTrue(r.ok)
        self.assertEqual(r.answer, "结果 2")
        tool_msgs = [m for m in r.messages if m.get("role") == "tool"]
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertEqual(tool_msgs[0]["content"], "calc:ok")
        types = [e["type"] for e in r.trace]
        self.assertIn("tool_call", types)
        self.assertIn("tool_observation", types)
        self.assertEqual(types[-1], "final")

    async def test_tool_failure_is_recoverable(self):
        p = ScriptedProvider([
            assistant_tools(call("c1", "bad", {})),
            final("换个方法完成"),
        ])
        r = await _loop(p, RecordingBus(fail={"bad"})).run("做事")
        self.assertTrue(r.ok)
        obs = [m for m in r.messages if m.get("role") == "tool"][0]
        self.assertTrue(obs["content"].startswith("错误"))
        self.assertEqual(r.answer, "换个方法完成")

    async def test_malformed_arguments_are_not_executed(self):
        bus = RecordingBus()
        p = ScriptedProvider([
            assistant_tools(call("c1", "calc", {}, raw="{bad json")),
            final("已修正"),
        ])
        r = await _loop(p, bus).run("算")
        self.assertEqual(bus.calls, [])
        obs = [m for m in r.messages if m.get("role") == "tool"][0]
        self.assertIn("不是合法 JSON", obs["content"])
        self.assertTrue(r.ok)

    async def test_non_object_arguments_are_rejected(self):
        bus = RecordingBus()
        p = ScriptedProvider([
            assistant_tools(call("c1", "calc", {}, raw="[1,2]")),
            final("ok"),
        ])
        await _loop(p, bus).run("算")
        self.assertEqual(bus.calls, [])
        obs = [m for m in p.calls[-1]["messages"] if m.get("role") == "tool"][0]
        self.assertIn("JSON 对象", obs["content"])

    async def test_max_steps_finalize(self):
        p = ScriptedProvider([assistant_tools(call("c1", "t", {}))], final="收尾")
        r = await _loop(p, RecordingBus(), max_steps=1).run("长任务")
        self.assertEqual(r.stopped_reason, "max_steps")
        self.assertEqual(r.answer, "收尾")
        # 第二次调用禁用工具
        self.assertEqual(p.calls[-1]["tools"], [])

    async def test_timeout(self):
        p = ScriptedProvider(delay=0.2)
        r = await _loop(p, RecordingBus(), timeout=0.05).run("慢任务")
        self.assertEqual(r.stopped_reason, "timeout")
        self.assertIn("超时", r.error)
        self.assertFalse(r.ok)

    async def test_interrupt_between_steps(self):
        holder = {}

        async def on_event(ev):
            if ev.get("type") == "tool_observation":
                holder["loop"].interrupt("用户停止")

        p = ScriptedProvider([assistant_tools(call("c1", "t", {}))])
        lp = _loop(p, RecordingBus(), on_event=on_event)
        holder["loop"] = lp
        r = await lp.run("任务")
        self.assertEqual(r.stopped_reason, "interrupted")
        self.assertTrue(r.interrupted)
        # 已产生的中间结果不丢：回退到最后一次工具观测
        self.assertEqual(r.answer, "t:ok")
        self.assertIn("interrupt", [e["type"] for e in r.trace])

    async def test_steering_injected_before_next_step(self):
        async def on_event(ev):
            if ev.get("type") == "tool_observation":
                holder["loop"].steer("改为输出表格")

        holder = {}
        p = ScriptedProvider([
            assistant_tools(call("c1", "t", {})),
            final("按新要求完成"),
        ])
        lp = _loop(p, RecordingBus(), on_event=on_event)
        holder["loop"] = lp
        r = await lp.run("任务")
        self.assertTrue(r.ok)
        second_msgs = p.calls[1]["messages"]
        self.assertTrue(any("改为输出表格" in str(m.get("content"))
                            for m in second_msgs))
        self.assertIn("steer", [e["type"] for e in r.trace])

    async def test_parallel_readonly_calls(self):
        bus = RecordingBus(parallel=True, sleep=0.05)
        p = ScriptedProvider([
            assistant_tools(call("c1", "r1", {}), call("c2", "r2", {})),
            final("ok"),
        ])
        await _loop(p, bus).run("并行")
        self.assertEqual(bus.max_active, 2)

    async def test_side_effect_calls_are_serial(self):
        bus = RecordingBus(parallel=False, sleep=0.02)
        p = ScriptedProvider([
            assistant_tools(call("c1", "w1", {}), call("c2", "w2", {})),
            final("ok"),
        ])
        await _loop(p, bus).run("串行")
        self.assertEqual(bus.max_active, 1)
        self.assertEqual([c[0] for c in bus.calls], ["w1", "w2"])

    async def test_repeated_failure_hint(self):
        same = assistant_tools(call("c1", "bad", {"x": 1}))
        p = ScriptedProvider([same, dict(same), final("换方法完成")])
        r = await _loop(p, RecordingBus(fail={"bad"})).run("做事")
        last_msgs = p.calls[-1]["messages"]
        self.assertTrue(any("连续失败" in str(m.get("content")) for m in last_msgs))
        self.assertIn("failure_hint", [e["type"] for e in r.trace])

    async def test_tool_output_aging(self):
        class LongBus(RecordingBus):
            async def execute(self, name, args, ctx=None):
                self.calls.append((name, args))
                return {"ok": True, "result": "X" * 500 if name == "a" else "Y" * 500}

        bus = LongBus(parallel=True)
        p = ScriptedProvider([
            assistant_tools(call("c1", "a", {}), call("c2", "b", {})),
            final("done"),
        ])
        lp = _loop(p, bus, settings={"loop.tool_age_after": 1,
                                     "loop.tool_age_chars": 100})
        await lp.run("go")
        sent = p.calls[-1]["messages"]
        tools = [m for m in sent if m.get("role") == "tool"]
        self.assertIn("较早的工具输出已折叠", tools[0]["content"])
        self.assertEqual(tools[1]["content"], "Y" * 500)


class HelperTests(unittest.TestCase):
    def test_failure_hint_none_on_success(self):
        msgs = [
            {"role": "assistant", "tool_calls": [call("c1", "t", {})]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        self.assertEqual(_failure_hint(msgs), "")

    def test_age_keeps_recent(self):
        msgs = [{"role": "tool", "content": "A" * 500},
                {"role": "tool", "content": "B" * 500}]
        out = age_tool_outputs(msgs, keep=1, max_chars=100)
        self.assertIn("折叠", out[0]["content"])
        self.assertEqual(out[1]["content"], "B" * 500)


if __name__ == "__main__":
    unittest.main()
