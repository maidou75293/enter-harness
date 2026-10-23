"""OpenTelemetry 导出：span 映射、标量过滤、延迟依赖与异常策略。"""
import sys
import unittest
from unittest import mock

from enter_harness.domain import Event
from enter_harness.errors import ConfigError
from enter_harness.observability import OtelEventSink, event_to_attributes


class FakeSpan:
    """最小 span：记录可选的 set_attribute / add_event 调用。"""

    def __init__(self):
        self.attributes = {}
        self.events = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        self.events.append((name, dict(attributes or {})))


class FakeSpanContext:
    """上下文管理器：进入时返回 span。"""

    def __init__(self, span):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeTracer:
    """鸭子类型 tracer：记录每次 span 的名称与属性。"""

    def __init__(self, error=None):
        self.calls = []
        self.spans = []
        self.error = error

    def start_as_current_span(self, name, attributes=None):
        if self.error is not None:
            raise self.error
        span = FakeSpan()
        self.calls.append((name, dict(attributes or {})))
        self.spans.append(span)
        return FakeSpanContext(span)


class OtelEventSinkTests(unittest.TestCase):
    def test_span_name_uses_agent_prefix(self):
        tracer = FakeTracer()
        OtelEventSink(tracer).emit(Event("run.started", payload={}))
        self.assertEqual(tracer.calls[0][0], "agent.run.started")

    def test_attributes_include_run_trace_and_scalar_payload(self):
        tracer = FakeTracer()
        event = Event("tool.called", {"tool": "search", "step": 2},
                      run_id="r-1", trace_id="t-1")
        OtelEventSink(tracer).emit(event)
        _, attrs = tracer.calls[0]
        self.assertEqual(attrs["agent.run_id"], "r-1")
        self.assertEqual(attrs["agent.trace_id"], "t-1")
        self.assertEqual(attrs["agent.tool"], "search")
        self.assertEqual(attrs["agent.step"], 2)

    def test_scalar_passthrough_and_non_scalar_conversion(self):
        attrs = event_to_attributes(Event("x", {
            "s": "v", "i": 1, "f": 1.5, "b": True,
            "nested": {"k": 1}, "items": [1, 2], "none": None,
        }))
        self.assertEqual(attrs["agent.s"], "v")
        self.assertEqual(attrs["agent.i"], 1)
        self.assertEqual(attrs["agent.f"], 1.5)
        self.assertIs(attrs["agent.b"], True)
        self.assertEqual(attrs["agent.nested"], "{'k': 1}")
        self.assertEqual(attrs["agent.items"], "[1, 2]")
        self.assertNotIn("agent.none", attrs)

    def test_empty_run_and_trace_are_omitted(self):
        attrs = event_to_attributes(Event("x", {"k": 1}))
        self.assertNotIn("agent.run_id", attrs)
        self.assertNotIn("agent.trace_id", attrs)
        self.assertEqual(attrs, {"agent.k": 1})

    def test_missing_tracer_without_opentelemetry_raises_config_error(self):
        # sys.modules 中置 None 等价于“模块不可导入”，无需真的卸载依赖
        with mock.patch.dict(sys.modules, {"opentelemetry": None}):
            with self.assertRaises(ConfigError) as ctx:
                OtelEventSink()
        self.assertIn("pip install opentelemetry-api", str(ctx.exception))

    def test_emit_swallows_export_failure_by_default(self):
        sink = OtelEventSink(FakeTracer(error=RuntimeError("boom")))
        sink.emit(Event("x", {}))  # 不应抛出

    def test_strict_mode_reraises_export_failure(self):
        sink = OtelEventSink(FakeTracer(error=RuntimeError("boom")), strict=True)
        with self.assertRaises(RuntimeError):
            sink.emit(Event("x", {}))


if __name__ == "__main__":
    unittest.main()
