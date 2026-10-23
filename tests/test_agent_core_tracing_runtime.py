"""Trace / 中断 / 运行期上下文。"""
import asyncio
import unittest

from agent_core import Tracer
from agent_core.loop import AgentLoop
from agent_core.runtime import (bind, current_depth, current_run, current_session,
                                current_tenant, current_user, snapshot)

from core_fakes import RecordingBus, ScriptedProvider


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_seq_payload_and_sink(self):
        got = []

        async def sink(ev):
            got.append(ev)

        t = Tracer(sink=sink)
        await t.emit("a", x=1)
        await t.emit("b", y=2)
        self.assertEqual([e.seq for e in t.events], [1, 2])
        self.assertEqual(t.events[0].to_dict()["x"], 1)
        self.assertEqual([e["type"] for e in got], ["a", "b"])

    async def test_sync_sink(self):
        got = []
        t = Tracer(sink=lambda ev: got.append(ev))
        await t.emit("a")
        self.assertEqual(got[0]["type"], "a")

    async def test_max_events_ring(self):
        t = Tracer(max_events=3)
        for i in range(6):
            await t.emit("x", i=i)
        self.assertEqual([e.data["i"] for e in t.events], [3, 4, 5])
        self.assertEqual(t.events[-1].seq, 6)

    async def test_record_trims_and_no_sink(self):
        got = []
        t = Tracer(sink=lambda ev: got.append(ev), max_events=2)
        t.record("a")
        t.record("b")
        t.record("c")
        self.assertEqual([e.type for e in t.events], ["b", "c"])
        self.assertEqual(got, [])

    async def test_replay_last_clear(self):
        t = Tracer()
        for i in range(5):
            await t.emit("x", i=i)
        self.assertEqual([e.data["i"] for e in t.replay(start=2, end=3)], [1, 2])
        self.assertEqual(t.last("x").data["i"], 4)
        t.clear()
        self.assertEqual(t.events, [])
        self.assertEqual(t.last(), None)


class RuntimeTests(unittest.TestCase):
    def test_bind_restores_nested(self):
        current_tenant.set("t0")
        current_session.set("s0")
        with bind(tenant="t1", session="s1", run="r1", user="u1", depth=1):
            self.assertEqual(current_tenant.get(), "t1")
            with bind(tenant="t2", depth=2):
                self.assertEqual(current_tenant.get(), "t2")
                self.assertEqual(current_depth.get(), 2)
            self.assertEqual(current_tenant.get(), "t1")
            self.assertEqual(current_depth.get(), 1)
        self.assertEqual(current_tenant.get(), "t0")
        self.assertEqual(current_session.get(), "s0")
        self.assertEqual(current_user.get(), "")

    def test_normalize_snapshot(self):
        current_tenant.set("default")
        with bind(tenant="bad/../id"):
            self.assertEqual(current_tenant.get(), "default")
        self.assertEqual(snapshot().tenant, "default")


class InterruptTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_flag_and_clear(self):
        lp = AgentLoop(ScriptedProvider(), RecordingBus())
        self.assertFalse(lp.interrupted)
        lp.interrupt("stop")
        self.assertTrue(lp.interrupted)
        lp.clear_interrupt()
        self.assertFalse(lp.interrupted)

    async def test_steering_queue(self):
        lp = AgentLoop(ScriptedProvider(), RecordingBus())
        lp.steer("  one  ")
        lp.steer("")
        lp.steer("two")
        self.assertEqual(list(lp._steer), ["one", "two"])
        lp.clear_steering()
        self.assertEqual(list(lp._steer), [])


if __name__ == "__main__":
    unittest.main()
