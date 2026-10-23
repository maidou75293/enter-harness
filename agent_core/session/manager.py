"""SessionManager：会话 + 消息的领域封装（在 Store 之上加一点语义）。

只依赖 Store 的会话/消息方法，不关心业务字段。
"""
from __future__ import annotations

import uuid


class SessionManager:
    def __init__(self, store):
        self.store = store

    def create(self, title: str = "", sid: str | None = None, tenant: str = "default") -> dict:
        sid = sid or "s_" + uuid.uuid4().hex[:12]
        return self.store.create_session(sid, title, tenant)

    def list(self, tenant: str | None = None) -> list:
        return self.store.list_sessions(tenant)

    def get(self, sid: str):
        return self.store.get_session(sid)

    def delete(self, sid: str):
        self.store.delete_session(sid)

    def add(self, sid: str, role: str, content: str, meta: dict | None = None) -> int:
        return self.store.add_message(sid, role, content, meta)

    def history(self, sid: str, limit: int | None = None) -> list:
        return self.store.list_messages(sid, limit)

    def context_history(self, sid: str, limit: int | None = None) -> list:
        """给模型的上下文历史：排除已压缩的原始消息。"""
        return self.store.list_active_messages(sid, limit)

    def ensure(self, sid: str, tenant: str = "default") -> dict:
        s = self.get(sid)
        if s is None:
            s = self.create(sid=sid, tenant=tenant)
        return s
