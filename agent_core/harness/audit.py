"""默认审计出口：SQLite 追加表 + 哈希链。

合规约束落在这里：
- 只追加，不提供 update；
- 每条记录带 prev_hash/hash，可校验是否被改过；
- 默认留存 180 天，清理必须显式调用 prune。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from typing import Any, Callable, Sequence

from .types import AuditEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit(
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT '',
  decision TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  trace_id TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL,
  prev_hash TEXT NOT NULL DEFAULT '',
  hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit(tenant_id, at);
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit(trace_id);
"""

_FIELDS = ("id", "tenant_id", "actor", "action", "target", "decision",
           "reason", "trace_id", "at", "prev_hash", "hash")


class SQLiteAuditPort:
    """默认实现：单文件 / 内存 SQLite；产品可换远端审计库。"""

    def __init__(self, path: str = ":memory:", *, retention_days: int = 180,
                 clock: Callable[[], float] | None = None):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.retention_days = int(retention_days)
        self._clock = clock or time.time
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 写
    def emit(self, event: AuditEvent | dict) -> AuditEvent:
        if isinstance(event, AuditEvent):
            ev = event
        else:
            data = dict(event)
            if "at" in data and "created_at" not in data:
                data["created_at"] = data.pop("at")
            ev = AuditEvent(**data)
        if not ev.created_at:
            ev.created_at = self._clock()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT hash FROM audit ORDER BY at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            ev.prev_hash = str(row["hash"]) if row else ""
            ev.hash = self._digest(ev)
            self.conn.execute(
                f"INSERT INTO audit({','.join(_FIELDS)}) VALUES "
                f"({','.join('?' for _ in _FIELDS)})",
                tuple(getattr(ev, f) for f in _FIELDS))
        return ev

    # ------------------------------------------------------------ 读
    def query(self, filter: Any = None) -> Sequence[AuditEvent]:
        if callable(filter):
            rows = self.conn.execute(
                "SELECT * FROM audit ORDER BY at ASC, rowid ASC").fetchall()
            return [self._row(r) for r in rows if filter(self._row(r))]
        f = dict(filter or {})
        sql = ["SELECT * FROM audit WHERE 1=1"]
        args: list = []
        for key in ("tenant_id", "actor", "action", "decision", "trace_id"):
            if f.get(key) not in (None, ""):
                sql.append(f"AND {key}=?")
                args.append(str(f[key]))
        if f.get("since"):
            sql.append("AND at>=?")
            args.append(float(f["since"]))
        if f.get("until"):
            sql.append("AND at<=?")
            args.append(float(f["until"]))
        sql.append("ORDER BY at ASC, rowid ASC")
        sql.append("LIMIT ?")
        args.append(int(f.get("limit") or 1000))
        return [self._row(r) for r in self.conn.execute(" ".join(sql), args)]

    # ------------------------------------------------------------ 校验
    def verify_chain(self) -> dict:
        """重算整条链；返回 {ok, checked, broken_at}。"""
        rows = self.conn.execute(
            "SELECT * FROM audit ORDER BY at ASC, rowid ASC").fetchall()
        prev = ""
        checked = 0
        first = True
        for row in rows:
            ev = self._row(row)
            # 允许从保留窗口的起点开始校验：首条不再要求 prev_hash 存在，
            # 但仍重算本条 hash；否则 prune 之后必然误报。
            if (not first and ev.prev_hash != prev) or ev.hash != self._digest(ev):
                return {"ok": False, "checked": checked, "broken_at": ev.id}
            prev = ev.hash
            checked += 1
            first = False
        return {"ok": True, "checked": checked, "broken_at": ""}

    # ------------------------------------------------------------ 留存
    def prune(self, before: float | None = None) -> int:
        """清理 before 之前的记录（默认按 retention_days 算）；显式调用。"""
        cutoff = float(before if before is not None else
                       self._clock() - self.retention_days * 86400)
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM audit WHERE at < ?", (cutoff,))
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _digest(ev: AuditEvent) -> str:
        return hashlib.sha256(ev.canonical().encode("utf-8")).hexdigest()

    @staticmethod
    def _row(row) -> AuditEvent:
        d = dict(row)
        data = {k: d.get(k) for k in _FIELDS
                if k in AuditEvent.__dataclass_fields__ and k != "at"}
        data["created_at"] = d.get("created_at") or d.get("at") or 0
        return AuditEvent(**data)


# 兼容旧名；新代码统一用 SQLiteAuditPort
SQLiteAuditSink = SQLiteAuditPort
