"""默认本地存储（SQLite）：实现内核的写/读/归档。

只依赖标准库；产品可以不用它，自己实现 MemoryPort。
内核字段 14 个 + 可选扩展字段同表存放，缺省不影响语义。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from .model import (MemoryRecord, WriteResult, content_hash, normalize_scope,
                    normalize_status, validate_record)

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory(
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
  created_at REAL NOT NULL,
  content_hash TEXT NOT NULL,
  superseded_by TEXT NOT NULL DEFAULT '',
  subtype TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  condition TEXT NOT NULL DEFAULT '{}',
  status_reason TEXT NOT NULL DEFAULT '',
  provenance TEXT NOT NULL DEFAULT '[]',
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.6,
  expires_at REAL,
  updated_at REAL NOT NULL DEFAULT 0,
  last_used_at REAL NOT NULL DEFAULT 0,
  use_count INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1
);
-- R1：同 (tenant, scope, scope_id, agent_id, kind, key, condition) 只有一条 active
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_active
  ON memory(tenant_id, scope, scope_id, agent_id, kind, key, condition)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_memory_scope
  ON memory(tenant_id, scope, scope_id, agent_id, status);
"""

_COLS = ("id, tenant_id, kind, subtype, scope, scope_id, agent_id, session_id, "
         "key, content, summary, outcome, condition, source, importance, confidence, "
         "status, status_reason, superseded_by, expires_at, created_at, updated_at, "
         "last_used_at, use_count, content_hash, version, provenance")


def _cond_json(cond: dict) -> str:
    return json.dumps(cond or {}, sort_keys=True, ensure_ascii=False)


def _row_to_record(row) -> MemoryRecord:
    d = dict(row)
    try:
        d["condition"] = json.loads(d.get("condition") or "{}")
    except Exception:  # noqa: BLE001
        d["condition"] = {}
    try:
        d["provenance"] = json.loads(d.get("provenance") or "[]")
    except Exception:  # noqa: BLE001
        d["provenance"] = []
    return MemoryRecord(**{k: v for k, v in d.items()
                           if k in MemoryRecord.__dataclass_fields__})


class SQLiteMemoryStore:
    """默认实现：单文件 / 内存 SQLite。"""

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------ 写
    def upsert(self, item: MemoryRecord) -> WriteResult:
        err = validate_record(item)
        if err:
            return WriteResult(action="invalid", id=getattr(item, "id", ""),
                               reason=err, ok=False)
        cond = _cond_json(item.condition)
        now = time.time()
        item.updated_at = now
        if not item.created_at:
            item.created_at = now
        if not item.id:
            item.id = "m_" + uuid.uuid4().hex[:12]
        if not item.content_hash:
            item.content_hash = content_hash(item.summary or item.content)

        with self.conn:
            cur = self.conn.execute(
                "SELECT id, content_hash, provenance FROM memory "
                "WHERE tenant_id=? AND scope=? AND scope_id=? AND agent_id=? "
                "AND kind=? AND key=? AND condition=? AND status='active'",
                (item.tenant_id, item.scope, item.scope_id, item.agent_id,
                 item.kind, item.key, cond))
            row = cur.fetchone()
            if row is not None and row["content_hash"] == item.content_hash:
                prov = json.loads(row["provenance"] or "[]")
                if item.source and item.source not in [p.get("source") for p in prov]:
                    prov.append({"source": item.source, "at": now})
                self.conn.execute(
                    "UPDATE memory SET provenance=?, use_count=use_count+1, "
                    "last_used_at=? WHERE id=?",
                    (json.dumps(prov, ensure_ascii=False), now, row["id"]))
                return WriteResult(action="noop", id=row["id"])
            replaced_id = ""
            if row is not None:
                self.conn.execute(
                    "UPDATE memory SET status='superseded', status_reason='superseded', "
                    "superseded_by=?, updated_at=? WHERE id=?",
                    (item.id, now, row["id"]))
                action = "supersede"
                replaced_id = row["id"]
            else:
                action = "append"
            prov = list(item.provenance or [])
            if item.source and item.source not in [p.get("source") for p in prov]:
                prov.append({"source": item.source, "at": now})
            self.conn.execute(
                f"INSERT INTO memory({_COLS}) VALUES "
                "(:id,:tenant_id,:kind,:subtype,:scope,:scope_id,:agent_id,:session_id,"
                ":key,:content,:summary,:outcome,:condition,:source,:importance,:confidence,"
                ":status,:status_reason,:superseded_by,:expires_at,:created_at,:updated_at,"
                ":last_used_at,:use_count,:content_hash,:version,:provenance)",
                {
                    "id": item.id, "tenant_id": item.tenant_id,
                    "kind": item.kind, "subtype": item.subtype,
                    "scope": item.scope, "scope_id": item.scope_id,
                    "agent_id": item.agent_id, "session_id": item.session_id,
                    "key": item.key, "content": item.content,
                    "summary": item.summary, "outcome": item.outcome,
                    "condition": cond, "source": item.source,
                    "importance": item.importance, "confidence": item.confidence,
                    "status": item.status, "status_reason": item.status_reason,
                    "superseded_by": item.superseded_by,
                    "expires_at": item.expires_at, "created_at": item.created_at,
                    "updated_at": item.updated_at, "last_used_at": item.last_used_at,
                    "use_count": item.use_count, "content_hash": item.content_hash,
                    "version": item.version,
                    "provenance": json.dumps(prov, ensure_ascii=False),
                })
        return WriteResult(action=action, id=item.id, replaced_id=replaced_id)

    # ------------------------------------------------------------ 读
    def query(self, tenant_id: str = "", scope: str = "", scope_id: str = "",
              agent_id: str | None = None, status: str = "active",
              include_expired: bool = False) -> list[MemoryRecord]:
        sql = ["SELECT * FROM memory WHERE tenant_id=? AND status=?"]
        args: list = [tenant_id, normalize_status(status)]
        if scope:
            sql.append("AND scope=?")
            args.append(normalize_scope(scope))
        if scope_id:
            sql.append("AND scope_id=?")
            args.append(scope_id)
        if agent_id is not None:
            sql.append("AND agent_id=?")
            args.append(agent_id)
        if not include_expired:
            now = time.time()
            sql.append("AND (expires_at IS NULL OR expires_at <= 0 OR expires_at > ?)")
            args.append(now)
        sql.append("ORDER BY created_at DESC")
        return [_row_to_record(r) for r in self.conn.execute(" ".join(sql), args)]

    def get(self, mid: str) -> MemoryRecord | None:
        row = self.conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
        return _row_to_record(row) if row else None

    # ------------------------------------------------------------ 归档
    def archive(self, ids: list[str], reason: str = "policy") -> int:
        if not ids:
            return 0
        now = time.time()
        with self.conn:
            n = 0
            for mid in ids:
                cur = self.conn.execute(
                    "UPDATE memory SET status='archived', status_reason=?, updated_at=? "
                    "WHERE id=? AND status='active'", (reason, now, mid))
                n += cur.rowcount
        return n

    def close(self) -> None:
        self.conn.close()
