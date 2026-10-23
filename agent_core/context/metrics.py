"""上下文拼装指标：把 ``assemble()`` 的返回值折算成可观测计数。

本模块只做纯计算：不读文件、不依赖第三方库，也不反向依赖 pipeline。

统计口径（与 pipeline 的 trace 约定对齐）：

- **注入**：未丢弃、且带 ``chars`` / ``est_tokens`` / ``cache`` 的块行；
- **丢弃**：``dropped=True`` 的行，按 ``reason`` 归类
  （budget / max_chars / denied / empty / duplicate_id / duplicate_text）；
- **诊断行**：``error`` / ``hash_mismatch`` 行不参与块计数，直接忽略；
- **authority / kind**：只统计实际注入的块；
- **缓存**：``cache=hit`` 记命中，``cache=fetched`` 记未命中；
  ``inline``（调用方直接给正文）不经过缓存，两者都不计。
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Iterable

__all__ = ["ContextMetrics", "MetricsRegistry", "summarize"]


def _ratio(used: int, budget: int) -> float:
    """预算利用率：预算为 0 时记 0，结果夹在 [0, 1]。"""
    if budget <= 0:
        return 0.0
    return min(1.0, max(0.0, float(used) / float(budget)))


def _merge_counts(left: dict, right: dict) -> dict:
    """合并两个计数字典，返回新字典（不改动入参）。"""
    out = dict(left or {})
    for key, value in (right or {}).items():
        out[key] = out.get(key, 0) + int(value or 0)
    return out


def _is_injected_row(row: dict) -> bool:
    """判断 trace 行是否代表一个成功注入的块。"""
    if row.get("dropped"):
        return False
    if "error" in row or "hash_mismatch" in row:
        return False
    return "chars" in row or "est_tokens" in row or "cache" in row


@dataclass
class ContextMetrics:
    """一次（或多次合并后）上下文拼装的可观测指标。"""

    blocks_total: int = 0                 # 参与决策的块总数 = 注入 + 丢弃
    blocks_injected: int = 0              # 实际进入上下文的块数
    blocks_dropped: int = 0               # 被丢弃的块数
    blocks_truncated: int = 0             # 被截断的块数（当前流水线整块丢弃，恒为 0）
    used_chars: int = 0                   # 注入正文的总字符数
    budget_chars: int = 0                 # 总预算字符数；0 表示未设预算
    budget_utilization: float = 0.0       # used_chars / budget_chars，0~1
    cache_hits: int = 0                   # 缓存命中次数
    cache_misses: int = 0                 # 缓存未命中次数（走了正文抓取）
    drop_reasons: dict[str, int] = field(default_factory=dict)   # 丢弃原因 → 次数
    authority_counts: dict[str, int] = field(default_factory=dict)  # 注入块权威档 → 次数
    injected_kinds: dict[str, int] = field(default_factory=dict)    # 注入块 kind → 次数

    @classmethod
    def from_assemble(cls, result: dict, budget_chars: int = 0) -> "ContextMetrics":
        """从 ``assemble()`` 的返回值计算指标。

        ``budget_chars`` 需调用方传入（assemble 的结果里没有预算字段）；
        传 0 时利用率记 0。
        """
        result = result or {}
        metrics = cls(budget_chars=max(int(budget_chars or 0), 0))

        for row in result.get("trace") or ():
            if not isinstance(row, dict):
                continue
            # 诊断行（抓取异常 / 指纹不一致）不改变块计数
            if "error" in row or "hash_mismatch" in row:
                continue

            if row.get("dropped"):
                metrics.blocks_dropped += 1
                reason = str(row.get("reason") or "unknown")
                metrics.drop_reasons[reason] = metrics.drop_reasons.get(reason, 0) + 1
            elif _is_injected_row(row):
                metrics.blocks_injected += 1
                kind = str(row.get("kind") or "unknown")
                authority = str(row.get("authority") or "unknown")
                metrics.injected_kinds[kind] = metrics.injected_kinds.get(kind, 0) + 1
                metrics.authority_counts[authority] = (
                    metrics.authority_counts.get(authority, 0) + 1)
                if row.get("truncated"):
                    metrics.blocks_truncated += 1
            else:
                continue

            # 只有真正走缓存取正文的行才带 cache 字段
            state = row.get("cache")
            if state == "hit":
                metrics.cache_hits += 1
            elif state == "fetched":
                metrics.cache_misses += 1

        metrics.blocks_total = metrics.blocks_injected + metrics.blocks_dropped
        metrics.used_chars = int(result.get("used_chars") or 0)
        metrics.budget_utilization = _ratio(metrics.used_chars, metrics.budget_chars)
        return metrics

    def merge(self, other: "ContextMetrics") -> "ContextMetrics":
        """累加计数并重算利用率；预算取第一个非零值，返回新对象。"""
        if other is None:  # 容错：允许接入方传空
            other = ContextMetrics()
        merged = ContextMetrics(
            blocks_total=self.blocks_total + other.blocks_total,
            blocks_injected=self.blocks_injected + other.blocks_injected,
            blocks_dropped=self.blocks_dropped + other.blocks_dropped,
            blocks_truncated=self.blocks_truncated + other.blocks_truncated,
            used_chars=self.used_chars + other.used_chars,
            budget_chars=self.budget_chars or other.budget_chars,
            cache_hits=self.cache_hits + other.cache_hits,
            cache_misses=self.cache_misses + other.cache_misses,
            drop_reasons=_merge_counts(self.drop_reasons, other.drop_reasons),
            authority_counts=_merge_counts(self.authority_counts,
                                           other.authority_counts),
            injected_kinds=_merge_counts(self.injected_kinds, other.injected_kinds),
        )
        # 利用率必须按合并后的 used / budget 重算，不能相加或取平均
        merged.budget_utilization = _ratio(merged.used_chars, merged.budget_chars)
        return merged

    def to_dict(self) -> dict:
        """转成普通 dict（深拷贝计数字典，便于直接序列化）。"""
        return asdict(self)


def summarize(rows: Iterable[ContextMetrics]) -> dict:
    """汇总多轮指标：返回合计字段 + 轮次数 + 平均利用率。

    ``budget_utilization`` 是合计后的整体利用率；
    ``avg_utilization`` 是各轮利用率的算术平均（空集合记 0）。
    """
    items = [r for r in (rows or ()) if r is not None]
    total = ContextMetrics()
    for item in items:
        total = total.merge(item)
    out = total.to_dict()
    out["count"] = len(items)
    out["avg_utilization"] = (
        sum(float(r.budget_utilization) for r in items) / len(items)
        if items else 0.0
    )
    out["max_utilization"] = max(
        (float(r.budget_utilization) for r in items), default=0.0)
    return out


class MetricsRegistry:
    """进程内指标聚合器：线程安全，可选保留最近 N 轮。

    只做计数与快照，不依赖存储、模型或第三方库；需要上报 OpenTelemetry
    时由宿主定时调用 ``snapshot()`` 转换，不在内核里绑定 SDK。
    """

    def __init__(self, max_items: int = 0):
        self._lock = threading.Lock()
        self._items: list[ContextMetrics] = []
        self._max_items = max(0, int(max_items or 0))

    def record(self, metrics: ContextMetrics | None) -> dict:
        """记录一轮并返回当前快照。"""
        if metrics is None:
            return self.snapshot()
        with self._lock:
            self._items.append(metrics)
            if self._max_items and len(self._items) > self._max_items:
                del self._items[:-self._max_items]
            return self._snapshot_locked()

    def record_assemble(self, result: dict, budget_chars: int = 0) -> dict:
        """从 ``assemble()`` 返回值直接记录一轮。"""
        return self.record(ContextMetrics.from_assemble(result, budget_chars))

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def reset(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def _snapshot_locked(self) -> dict:
        out = summarize(self._items)
        out["window"] = len(self._items)
        return out
