"""PostgreSQL 记忆：R1 唯一索引 + 四态写入 + 租户/范围过滤。"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Sequence

from ..context.model import (MemoryRecord, WriteResult, content_hash,
                             normalize_scope, normalize_status, validate_record)
from .pg import PgStore, rows

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS harness_memory(
      id TEXT PRIMARY KEY,
      tenant_id TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL,
      scope TEXT NOT NULL,
      scope_id TEXT NOT NULL DEFAULT '',
      agent_id TEXT NOT NULL DEFAULT '',
      session_id TEXT NOT NULL DEFAULT '',
      key TEXT NOT NULL DEFAULT '',
      content TEXT NOT NULL,
      source TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'active',
      created_at DOUBLE PRECISION NOT NULL,
      content_hash TEXT NOT NULL,
      superseded_by TEXT NOT NULL DEFAULT '',
      subtype TEXT NOT NULL DEFAULT '',
      summary TEXT NOT NULL DEFAULT '',
      outcome TEXT NOT NULL DEFAULT '',
      condition JSONB NOT NULL DEFAULT '{}'::jsonb,
      status_reason TEXT NOT NULL DEFAULT '',
      provenance JSONB NOT NULL DEFAULT '[]'::jsonb,
      importance DOUBLE PRECISION NOT NULL DEFAULT 0.5,
      confidence DOUBLE PRECISION NOT NULL DEFAULT 0.6,
      expires_at DOUBLE PRECISION,
      updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
      last_used_at DOUBLE PRECISION NOT NULL DEFAULT 0,
      use_count INTEGER NOT NULL DEFAULT 0,
      version INTEGER NOT NULL DEFAULT 1,
      extra JSONB NOT NULL DEFAULT '{}'::jsonb
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_harness_memory_active
       ON harness_memory(tenant_id, scope, scope_id, agent_id, kind, key, condition)
       WHERE status='active'""",
    """CREATE INDEX IF NOT EXISTS idx_harness_memory_scope
       ON harness_memory(tenant_id, scope, scope_id, agent_id, status)""",
]

_COLS = ("id, tenant_id, kind, subtype, scope, scope_id, agent_id, session_id, "
         "key, content, summary, outcome, condition, source, importance, confidence, "
         "status, status_reason, superseded_by, expires_at, created_at, updated_at, "
         "last_used_at, use_count, content_hash, version, provenance, extra")


def _cond_json(cond: dict) -> str:
    return json.dumps(cond or {}, sort_keys=True, ensure_ascii=False)


def _lock_id(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8],
                          "big", signed=True)


def _to_record(row: dict) -> MemoryRecord:
    data = {k: v for k, v in row.items()
            if k in MemoryRecord.__dataclass_fields__}
    if not isinstance(data.get("condition"), dict):
        data["condition"] = {}
    if not isinstance(data.get("provenance"), list):
        data["provenance"] = []
    if not isinstance(data.get("extra"), dict):
        data["extra"] = {}
    return MemoryRecord(**data)


class PostgresMemoryPort:
    def __init__(self, dsn: str = "", *, conn=None):
        self.db = PgStore(dsn, conn=conn)
        self.db.setup(SCHEMA)

    # ------------------------------------------------------------ 写
    def upsert(self, item: MemoryRecord) -> WriteResult:
        err = validate_record(item)
        if err:
            return WriteResult(action="invalid", id=getattr(item, "id", ""),
                               reason=err, ok=False)
        item.id = item.id or ("m_" + uuid.uuid4().hex[:12])
        item.content_hash = item.content_hash or content_hash(
            item.summary or item.content)
        now = time.time()
        item.updated_at = now
        item.created_at = item.created_at or now
        cond = _cond_json(item.condition)
        lock_key = "|".join([item.tenant_id, item.scope, item.scope_id,
                             item.agent_id, item.kind, item.key, cond])

        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(lock_key),))
            cur = conn.execute(
                "SELECT id, content_hash, provenance FROM harness_memory "
                "WHERE tenant_id=%s AND scope=%s AND scope_id=%s AND agent_id=%s "
                "AND kind=%s AND key=%s AND condition=%s::jsonb AND status='active'",
                (item.tenant_id, item.scope, item.scope_id, item.agent_id,
                 item.kind, item.key, cond))
            existing = rows(cur)
            row = existing[0] if existing else None
            if row is not None and row["content_hash"] == item.content_hash:
                prov = row.get("provenance") or []
                if not isinstance(prov, list):
                    prov = []
                if item.source and item.source not in [p.get("source") for p in prov]:
                    prov.append({"source": item.source, "at": now})
                conn.execute(
                    "UPDATE harness_memory SET provenance=%s::jsonb, "
                    "use_count=use_count+1, last_used_at=%s WHERE id=%s",
                    (json.dumps(prov, ensure_ascii=False), now, row["id"]))
                return WriteResult(action="noop", id=row["id"])

            replaced_id = ""
            action = "append"
            if row is not None:
                conn.execute(
                    "UPDATE harness_memory SET status='superseded', "
                    "status_reason='superseded', superseded_by=%s, updated_at=%s "
                    "WHERE id=%s", (item.id, now, row["id"]))
                replaced_id = row["id"]
                action = "supersede"

            prov = list(item.provenance or [])
            if item.source and item.source not in [p.get("source") for p in prov]:
                prov.append({"source": item.source, "at": now})
            conn.execute(
                f"INSERT INTO harness_memory({_COLS}) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,"
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
                (item.id, item.tenant_id, item.kind, item.subtype, item.scope,
                 item.scope_id, item.agent_id, item.session_id, item.key,
                 item.content, item.summary, item.outcome, cond, item.source,
                 item.importance, item.confidence, item.status,
                 item.status_reason, item.superseded_by, item.expires_at,
                 item.created_at, item.updated_at, item.last_used_at,
                 item.use_count, item.content_hash, item.version,
                 json.dumps(prov, ensure_ascii=False),
                 json.dumps(item.extra or {}, ensure_ascii=False)))
        return WriteResult(action=action, id=item.id, replaced_id=replaced_id)

    # ------------------------------------------------------------ 读
    def query(self, req=None) -> Sequence[MemoryRecord]:
        req = dict(req or {})
        tenant_id = str(req.get("tenant_id") or "")
        status = normalize_status(req.get("status") or "active")
        sql = ["SELECT * FROM harness_memory WHERE tenant_id=%s AND status=%s"]
        args: list = [tenant_id, status]
        if req.get("scope"):
            sql.append("AND scope=%s")
            args.append(normalize_scope(req.get("scope")))
        if req.get("scope_id"):
            sql.append("AND scope_id=%s")
            args.append(str(req.get("scope_id")))
        if "agent_id" in req:
            sql.append("AND agent_id=%s")
            args.append(str(req.get("agent_id") or ""))
        if status == "active" and not req.get("include_expired", False):
            now = time.time()
            sql.append("AND (expires_at IS NULL OR expires_at <= 0 OR expires_at > %s)")
            args.append(now)
        if req.get("query"):
            sql.append("AND (content ILIKE %s OR summary ILIKE %s OR key ILIKE %s)")
            like = f"%{req['query']}%"
            args.extend([like, like, like])
        sql.append("ORDER BY updated_at DESC")
        return [_to_record(r) for r in self.db.fetchall(" ".join(sql), args)]

    def get(self, mid: str) -> MemoryRecord | None:
        row = self.db.fetchone("SELECT * FROM harness_memory WHERE id=%s", (mid,))
        return _to_record(row) if row else None

    def archive(self, ids: Sequence[str], reason: str = "policy") -> int:
        wanted = [str(x) for x in (ids or ()) if str(x)]
        if not wanted:
            return 0
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE harness_memory SET status='archived', status_reason=%s, "
                "updated_at=%s WHERE id = ANY(%s) AND status='active'",
                (reason, time.time(), wanted))
            return int(cur.rowcount or 0)

    def close(self) -> None:
        self.db.close()
