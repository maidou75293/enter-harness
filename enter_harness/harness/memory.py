"""MemoryPort 适配器：把默认 SQLite 存储接到统一查询契约上。

SQLiteMemoryStore 的 query 是「按列过滤」，端口是「按请求对象」；
适配器负责这层翻译，避免每个产品自己写一遍。
"""
from __future__ import annotations

from typing import Any, Sequence

from ..context.model import MemoryRecord, WriteResult
from .write import write_record


class StoreMemoryPort:
    def __init__(self, store):
        self.store = store

    def query(self, req: Any = None) -> Sequence[MemoryRecord]:
        r = dict(req or {})
        kwargs = {
            "tenant_id": str(r.get("tenant_id") or ""),
            "scope": str(r.get("scope") or ""),
            "scope_id": str(r.get("scope_id") or ""),
            "status": str(r.get("status") or "active"),
            "include_expired": bool(r.get("include_expired", False)),
        }
        if "agent_id" in r:
            kwargs["agent_id"] = str(r.get("agent_id") or "")
        return self.store.query(**kwargs)

    def upsert(self, item: MemoryRecord) -> WriteResult:
        return write_record(self.store, item)

    def archive(self, ids: Sequence[str], reason: str = "policy") -> int:
        return self.store.archive(list(ids), reason=reason)

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
