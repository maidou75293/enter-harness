"""PostgreSQL 审计：只追加 + 哈希链 + 查询 + 留存清理。"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Sequence

from .pg import PgStore
from .types import AuditEvent

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS harness_audit(
      id TEXT PRIMARY KEY,
      tenant_id TEXT NOT NULL DEFAULT '',
      actor TEXT NOT NULL DEFAULT '',
      action TEXT NOT NULL DEFAULT '',
      target TEXT NOT NULL DEFAULT '',
      decision TEXT NOT NULL DEFAULT '',
      reason TEXT NOT NULL DEFAULT '',
      trace_id TEXT NOT NULL DEFAULT '',
      created_at DOUBLE PRECISION NOT NULL,
      prev_hash TEXT NOT NULL DEFAULT '',
      hash TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE INDEX IF NOT EXISTS idx_harness_audit_tenant
       ON harness_audit(tenant_id, created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_harness_audit_trace
       ON harness_audit(trace_id)""",
]


class PostgresAuditPort:
    def __init__(self, dsn: str = "", *, conn=None, retention_days: int = 180):
        self.db = PgStore(dsn, conn=conn)
        self.db.setup(SCHEMA)
        self.retention_days = int(retention_days)

    def emit(self, event: AuditEvent | dict) -> AuditEvent:
        if isinstance(event, AuditEvent):
            ev = event
        else:
            data = dict(event)
            if "at" in data and "created_at" not in data:
                data["created_at"] = data.pop("at")
            ev = AuditEvent(**data)
        if not ev.created_at:
            ev.created_at = time.time()
        with self.db.transaction() as conn:
            # 串行化哈希链：同一事务内锁住审计流的最后一个 hash
            conn.execute("SELECT pg_advisory_xact_lock(%s)",
                         (int.from_bytes(hashlib.sha256(
                             b"harness_audit").digest()[:8], "big", signed=True),))
            cur = conn.execute(
                "SELECT hash FROM harness_audit "
                "ORDER BY created_at DESC, id DESC LIMIT 1")
            last = cur.fetchone()
            ev.prev_hash = str(last[0]) if last else ""
            ev.hash = self._digest(ev)
            conn.execute(
                "INSERT INTO harness_audit(id, tenant_id, actor, action, target, "
                "decision, reason, trace_id, created_at, prev_hash, hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (ev.id, ev.tenant_id, ev.actor, ev.action, ev.target,
                 ev.decision, ev.reason, ev.trace_id, ev.created_at,
                 ev.prev_hash, ev.hash))
        return ev

    def query(self, filter: Any = None) -> Sequence[AuditEvent]:
        f = dict(filter or {})
        sql = ["SELECT * FROM harness_audit WHERE 1=1"]
        args: list = []
        for key in ("tenant_id", "actor", "action", "decision", "trace_id"):
            if f.get(key) not in (None, ""):
                sql.append(f"AND {key}=%s")
                args.append(str(f[key]))
        if f.get("since"):
            sql.append("AND created_at>=%s")
            args.append(float(f["since"]))
        if f.get("until"):
            sql.append("AND created_at<=%s")
            args.append(float(f["until"]))
        sql.append("ORDER BY created_at ASC, id ASC LIMIT %s")
        args.append(int(f.get("limit") or 1000))
        return [self._event(r) for r in self.db.fetchall(" ".join(sql), args)]

    def verify_chain(self) -> dict:
        rows = self.db.fetchall(
            "SELECT * FROM harness_audit ORDER BY created_at ASC, id ASC")
        prev = ""
        checked = 0
        first = True
        for row in rows:
            ev = self._event(row)
            if (not first and ev.prev_hash != prev) or ev.hash != self._digest(ev):
                return {"ok": False, "checked": checked, "broken_at": ev.id}
            prev = ev.hash
            checked += 1
            first = False
        return {"ok": True, "checked": checked, "broken_at": ""}

    def prune(self, before: float | None = None) -> int:
        cutoff = float(before if before is not None else
                       time.time() - self.retention_days * 86400)
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM harness_audit WHERE created_at < %s", (cutoff,))
            return int(cur.rowcount or 0)

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _digest(ev: AuditEvent) -> str:
        return hashlib.sha256(ev.canonical().encode("utf-8")).hexdigest()

    @staticmethod
    def _event(row: dict) -> AuditEvent:
        return AuditEvent(
            id=str(row["id"]), tenant_id=str(row.get("tenant_id") or ""),
            actor=str(row.get("actor") or ""), action=str(row.get("action") or ""),
            target=str(row.get("target") or ""),
            decision=str(row.get("decision") or ""),
            reason=str(row.get("reason") or ""),
            trace_id=str(row.get("trace_id") or ""),
            created_at=float(row.get("created_at") or 0),
            prev_hash=str(row.get("prev_hash") or ""),
            hash=str(row.get("hash") or ""))
