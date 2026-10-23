"""默认采集实现：外部格式 → RawInput。

只做归集与字段补齐，不读记忆、不决定预算、不直接产出 Block。
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from .types import RawInput


class DefaultIngestPort:
    """把消息 / 工具结果 / 事件 / 附件指针统一成 RawInput。"""

    def __init__(self, *, default_scope: str = "", default_scope_id: str = ""):
        self.default_scope = default_scope
        self.default_scope_id = default_scope_id

    def collect(self, req: Any = None) -> Sequence[RawInput]:
        items = self._items(req)
        out = []
        for item in items:
            raw = self.normalize(item)
            if raw is not None and str(raw.content or "").strip():
                out.append(raw)
        return out

    # ------------------------------------------------------------ 归一
    def normalize(self, item: Any) -> RawInput | None:
        if item is None:
            return None
        if isinstance(item, RawInput):
            return self._fill(item)
        if isinstance(item, str):
            return self._fill(RawInput(source="user", content=item))
        if not isinstance(item, dict):
            content = getattr(item, "content", None)
            if content is None:
                content = str(item)
            return self._fill(RawInput(source="event", content=str(content),
                                       meta={"type": type(item).__name__}))
        if "content" in item:
            role = str(item.get("role") or item.get("source") or "user").lower()
            source = "agent" if role in ("assistant", "agent") else (
                "user" if role in ("user", "human") else role)
            return self._fill(RawInput(
                source=source,
                content=self._text(item.get("content")),
                kind_hint=str(item.get("kind_hint") or "episodic"),
                scope=str(item.get("scope") or ""),
                scope_id=str(item.get("scope_id") or ""),
                meta={k: v for k, v in item.items()
                      if k not in ("content", "role", "source", "kind_hint",
                                   "scope", "scope_id")},
                created_at=float(item.get("created_at") or item.get("at") or 0),
            ))
        if "tool" in item or "name" in item:
            name = str(item.get("tool") or item.get("name") or "")
            body = item.get("result", item.get("error", ""))
            return self._fill(RawInput(
                source="tool", content=self._text(body), kind_hint="episodic",
                meta={"tool": name, **{k: v for k, v in item.items()
                                       if k not in ("tool", "name", "result", "error")}},
                created_at=float(item.get("created_at") or item.get("at") or 0),
            ))
        return self._fill(RawInput(
            source=str(item.get("source") or "event"),
            content=self._text(item.get("event", item.get("data", item))),
            kind_hint=str(item.get("kind_hint") or ""),
            scope=str(item.get("scope") or ""),
            scope_id=str(item.get("scope_id") or ""),
            meta=dict(item.get("meta") or {}),
            created_at=float(item.get("created_at") or item.get("at") or 0),
        ))

    # ------------------------------------------------------------ 内部
    def _items(self, req: Any) -> list:
        if req is None:
            return []
        if isinstance(req, (list, tuple)):
            return list(req)
        if isinstance(req, dict):
            items = req.get("items")
            if isinstance(items, (list, tuple)):
                return list(items)
            if "items" in req and isinstance(items, dict):
                return [items]
            return [req]
        return [req]

    def _fill(self, raw: RawInput) -> RawInput:
        if not raw.scope:
            raw.scope = self.default_scope
        if not raw.scope_id:
            raw.scope_id = self.default_scope_id
        return raw

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            return str(value)
