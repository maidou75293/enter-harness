"""OpenTelemetry 事件导出（可选依赖）。

把内核 :class:`~enter_harness.domain.Event` 映射成 span：

- span 名：``agent.<event.type>``；
- 属性：payload 标量 + run_id / trace_id，统一加 ``agent.`` 前缀，
  避免与 OpenTelemetry 语义约定（semantic conventions）的键冲突。

设计约束：

- **不引入硬依赖**：模块本身不 import opentelemetry；只有构造
  ``OtelEventSink`` 且未显式传 tracer 时才延迟导入，未安装就抛
  :class:`~enter_harness.errors.ConfigError`（附 ``pip install opentelemetry-api`` 提示）。
- **观测失败不影响主链路**：``emit`` 默认吞掉内部异常；排障或测试可传
  ``strict=True`` 让异常原样抛出。
"""
from __future__ import annotations

from typing import Any

from ..domain import Event
from ..errors import ConfigError

__all__ = ["OtelEventSink", "event_to_attributes"]

_ATTR_PREFIX = "agent."


def _attribute_value(value: Any) -> str | int | float | bool | None:
    """把 payload 值转成 span 属性值；返回 None 表示该值应丢弃。"""
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    # dict / list / 自定义对象等：OpenTelemetry 只接受标量，统一转字符串
    return str(value)


def event_to_attributes(event: Event) -> dict[str, Any]:
    """把事件映射为 span 属性字典（纯函数，便于单测）。

    规则：

    - ``run_id`` / ``trace_id`` 非空时写入 ``agent.run_id`` / ``agent.trace_id``；
    - payload 中的标量（``str`` / ``int`` / ``float`` / ``bool``）原样保留；
    - 其他类型（dict / list 等）转成字符串；
    - ``None`` 丢弃；
    - 所有属性名统一加 ``agent.`` 前缀。
    """
    attrs: dict[str, Any] = {}
    run_id = getattr(event, "run_id", "")
    trace_id = getattr(event, "trace_id", "")
    if run_id:
        attrs[_ATTR_PREFIX + "run_id"] = str(run_id)
    if trace_id:
        attrs[_ATTR_PREFIX + "trace_id"] = str(trace_id)
    for key, value in (getattr(event, "payload", None) or {}).items():
        mapped = _attribute_value(value)
        if mapped is not None:
            attrs[f"{_ATTR_PREFIX}{key}"] = mapped
    return attrs


def _load_default_tracer() -> Any:
    """延迟导入 opentelemetry，返回 enter_harness 专用 tracer。"""
    try:
        from opentelemetry import trace  # 延迟导入：不装也能 import 本模块
    except ImportError as exc:
        raise ConfigError(
            "OtelEventSink 需要 opentelemetry-api，请执行 "
            "pip install opentelemetry-api 后重试"
        ) from exc
    return trace.get_tracer("enter_harness")


class OtelEventSink:
    """把内核事件导出为 OpenTelemetry span 的 EventSink 实现。

    参数：

    - ``tracer``：鸭子类型对象，需提供
      ``start_as_current_span(name, attributes=...)`` 并返回上下文管理器。
      span 对象可有 ``set_attribute`` / ``add_event``（本实现不依赖它们）。
      传 ``None`` 时使用 ``opentelemetry.trace.get_tracer("enter_harness")``。
    - ``strict``：为 ``True`` 时把导出异常向上抛；默认 ``False``，
      吞掉异常以保证主链路不受观测故障影响。

    用法::

        sink = OtelEventSink()
        sink.emit(Event("run.started", {"step": 1},
                        run_id="r1", trace_id="t1"))
    """

    def __init__(self, tracer: Any = None, *, strict: bool = False) -> None:
        self.tracer = tracer if tracer is not None else _load_default_tracer()
        self.strict = bool(strict)

    def emit(self, event: Event) -> None:
        """导出一个事件；默认不会向外抛异常。"""
        try:
            self._emit(event)
        except Exception:  # noqa: BLE001 —— 观测失败不能影响主流程
            if self.strict:
                raise

    def _emit(self, event: Event) -> None:
        """真正执行导出：开一个 span，属性写在 span 上。"""
        name = f"agent.{event.type}"
        attrs = event_to_attributes(event)
        # 进入/退出上下文即完成一次短 span；span 生命周期由 tracer 管理。
        with self.tracer.start_as_current_span(name, attributes=attrs):
            pass
