"""分层提示词注册表。

一次 system prompt = 多个「层」按 (layer, priority, name) 稳定排序拼接。
- 层：CORE(0) → RUNTIME(10) → MEMORY(20) → SKILLS(30) → TASK(40) → EXTRA(50)
- 同名再注册 = 覆写（业务层覆盖 core 默认值）
- section 支持动态回调（每次渲染重新求值，用于租户环境、手册等）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

LAYER_CORE = 0
LAYER_RUNTIME = 10
LAYER_MEMORY = 20
LAYER_SKILLS = 30
LAYER_TASK = 40
LAYER_EXTRA = 50


@dataclass
class PromptLayer:
    name: str
    layer: int = LAYER_CORE
    priority: int = 0
    text: str = ""
    provider: Callable[[], str] | None = field(default=None, repr=False)
    source: str = "core"

    def render(self) -> str:
        try:
            v = self.provider() if self.provider is not None else self.text
        except Exception:  # noqa: BLE001 —— 动态片段失败不应拖垮提示词
            return ""
        return (v or "").strip()


class PromptRegistry:
    def __init__(self):
        self._items: dict[str, PromptLayer] = {}

    # ---------------- 注册 / 覆写 ----------------
    def add(self, name: str, text: str, layer: int = LAYER_CORE, priority: int = 0,
            source: str = "core") -> "PromptRegistry":
        self._items[name] = PromptLayer(name, layer, priority, text=text, source=source)
        return self

    def override(self, name: str, text: str, *, layer: int | None = None,
                 priority: int | None = None, source: str = "app") -> "PromptRegistry":
        old = self._items.get(name)
        self._items[name] = PromptLayer(
            name, (old.layer if old else LAYER_CORE) if layer is None else layer,
            (old.priority if old else 0) if priority is None else priority,
            text=text, source=source)
        return self

    def section(self, name: str, provider: Callable[[], str], *,
                layer: int = LAYER_RUNTIME, priority: int = 0,
                source: str = "app") -> "PromptRegistry":
        """注册动态片段（每次 render 调用 provider）。"""
        self._items[name] = PromptLayer(name, layer, priority,
                                        provider=provider, source=source)
        return self

    def remove(self, name: str) -> None:
        self._items.pop(name, None)

    def get(self, name: str) -> str:
        item = self._items.get(name)
        return item.render() if item else ""

    def names(self) -> list:
        return [x.name for x in self._ordered()]

    def items(self) -> list:
        return [(x.name, x.layer, x.priority, x.source) for x in self._ordered()]

    # ---------------- 渲染 ----------------
    def _ordered(self) -> list:
        return sorted(self._items.values(),
                      key=lambda x: (x.layer, x.priority, x.name))

    def render(self, extra: str = "", separator: str = "\n\n") -> str:
        parts = [x.render() for x in self._ordered()]
        parts = [p for p in parts if p]
        if extra and extra.strip():
            parts.append(extra.strip())
        return separator.join(parts)
