"""Tracer：每一步可回放的结构化 run trace。

事件结构：{seq, ts, type, ...payload}
- 内存中保留（可设置 max_events 环形上限）
- 可选 sink：EventSink（async emit）或普通 callable（接收事件 dict），用于实时推送/落盘
- replay() 按 seq 顺序产出事件，支持区间，便于“回放每一步”
"""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class TraceEvent:
    seq: int
    ts: float
    type: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, **self.data}


class Tracer:
    def __init__(self, sink: Callable | Any = None, max_events: int = 10000):
        self.sink = sink
        self.max_events = int(max_events or 0)
        self._events: list[TraceEvent] = []
        self._seq = 0

    # ---------------- 记录 ----------------
    async def emit(self, event_type: str, **payload) -> TraceEvent:
        self._seq += 1
        ev = TraceEvent(self._seq, round(time.time(), 6), event_type, payload)
        self._events.append(ev)
        if self.max_events and len(self._events) > self.max_events:
            # 环形保留，但 seq 继续递增（回放仍有序）
            self._events = self._events[-self.max_events:]
        if self.sink is not None:
            d = ev.to_dict()
            try:
                r = self.sink.emit(d) if hasattr(self.sink, "emit") else self.sink(d)
                if inspect.isawaitable(r):
                    await r
            except Exception:  # noqa: BLE001 —— 落盘/推送失败不能影响主流程
                pass
        return ev

    def record(self, event_type: str, **payload) -> TraceEvent:
        """同步记录（不触发 sink），供非 async 组件使用。"""
        self._seq += 1
        ev = TraceEvent(self._seq, round(time.time(), 6), event_type, payload)
        self._events.append(ev)
        if self.max_events and len(self._events) > self.max_events:
            self._events = self._events[-self.max_events:]
        return ev

    # ---------------- 读取 ----------------
    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    def replay(self, start: int = 1, end: int | None = None, types: tuple | None = None):
        for ev in self._events:
            if ev.seq < start:
                continue
            if end is not None and ev.seq > end:
                break
            if types and ev.type not in types:
                continue
            yield ev

    def to_dicts(self) -> list[dict]:
        return [e.to_dict() for e in self._events]

    def last(self, event_type: str | None = None) -> TraceEvent | None:
        for ev in reversed(self._events):
            if event_type is None or ev.type == event_type:
                return ev
        return None

    def clear(self) -> None:
        self._events.clear()
        self._seq = 0
