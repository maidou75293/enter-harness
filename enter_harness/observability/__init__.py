"""可选观测导出（当前提供 OpenTelemetry）。"""
from .otel import OtelEventSink, event_to_attributes

__all__ = ["OtelEventSink", "event_to_attributes"]
