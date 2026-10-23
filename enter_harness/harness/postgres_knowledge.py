"""PostgreSQL 知识块：入库 / 审核 / 检索 / 回取。"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Sequence

from .knowledge import query_tokens
from .pg import PgStore

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS harness_knowledge_chunk(
      id TEXT PRIMARY KEY,
      tenant_id TEXT NOT NULL DEFAULT '',
      scope TEXT NOT NULL DEFAULT '',
      scope_id TEXT NOT NULL DEFAULT '',
      doc_id TEXT NOT NULL DEFAULT '',
      title TEXT NOT NULL DEFAULT '',
      source TEXT NOT NULL DEFAULT '',
      content TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      review_status TEXT NOT NULL DEFAULT 'pending',
      version TEXT NOT NULL DEFAULT '',
      created_at DOUBLE PRECISION NOT NULL,
      updated_at DOUBLE PRECISION NOT NULL,
      reviewer TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE INDEX IF NOT EXISTS idx_harness_chunk_scope
       ON harness_knowledge_chunk(tenant_id, scope, scope_id, review_status)""",
    """CREATE INDEX IF NOT EXISTS idx_harness_chunk_doc
       ON harness_knowledge_chunk(tenant_id, doc_id)""",
]

_STATUSES = ("pending", "approved", "rejected", "archived")


class PostgresKnowledgePort:
    def __init__(self, dsn: str = "", *, conn=None):
        self.db = PgStore(dsn, conn=conn)
        self.db.setup(SCHEMA)

    # ------------------------------------------------------------ 写
    def add(self, chunk: dict) -> str:
        data = dict(chunk or {})
        content = str(data.get("content") or "").strip()
        source = str(data.get("source") or "").strip()
        if not content:
            raise ValueError("content 不能为空")
        if not source:
            raise ValueError("source 不能为空")
        cid = str(data.get("id") or ("kb_" + uuid.uuid4().hex[:12]))
        tenant_id = str(data.get("tenant_id") or "")
        old = self.get_chunk(cid, tenant_id=tenant_id)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = time.time()
        status = str(data.get("review_status") or "").strip().lower()
        if old is not None:
            if digest == old.get("content_hash") and not status:
                status = str(old.get("review_status") or "pending")
            elif not status or status == str(old.get("review_status") or ""):
                status = "pending"
        if status not in _STATUSES:
            status = "pending"
        created = float((old or {}).get("created_at")
                        or data.get("created_at") or now)
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO harness_knowledge_chunk(
                     id, tenant_id, scope, scope_id, doc_id, title, source,
                     content, content_hash, review_status, version, created_at,
                     updated_at, reviewer)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(id) DO UPDATE SET
                     tenant_id=excluded.tenant_id, scope=excluded.scope,
                     scope_id=excluded.scope_id, doc_id=excluded.doc_id,
                     title=excluded.title, source=excluded.source,
                     content=excluded.content, content_hash=excluded.content_hash,
                     review_status=excluded.review_status, version=excluded.version,
                     updated_at=excluded.updated_at, reviewer=excluded.reviewer""",
                (cid, tenant_id, str(data.get("scope") or "project"),
                 str(data.get("scope_id") or ""), str(data.get("doc_id") or ""),
                 str(data.get("title") or ""), source, content, digest, status,
                 str(data.get("version") or ""), created, now,
                 str(data.get("reviewer") or (old or {}).get("reviewer") or "")))
        return cid

    def approve(self, chunk_id: str, reviewer: str = "") -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE harness_knowledge_chunk SET review_status='approved', "
                "reviewer=%s, updated_at=%s WHERE id=%s AND review_status<>'archived'",
                (str(reviewer), time.time(), str(chunk_id)))
            return bool(cur.rowcount)

    def archive(self, chunk_id: str) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE harness_knowledge_chunk SET review_status='archived', "
                "updated_at=%s WHERE id=%s", (time.time(), str(chunk_id)))
            return bool(cur.rowcount)

    # ------------------------------------------------------------ 读
    def search(self, query: str, top_k: int = 4, scope: str = "",
               tenant_id: str = "", scope_id: str = "") -> Sequence[dict]:
        sql = ["SELECT * FROM harness_knowledge_chunk "
               "WHERE review_status='approved' AND source<>''"]
        args: list = []
        if tenant_id:
            sql.append("AND tenant_id=%s")
            args.append(tenant_id)
        if scope:
            sql.append("AND scope=%s")
            args.append(scope)
        if scope_id:
            sql.append("AND scope_id=%s")
            args.append(scope_id)
        sql.append("ORDER BY updated_at DESC LIMIT 500")
        candidates = self.db.fetchall(" ".join(sql), args)

        q = str(query or "").strip()
        tokens = query_tokens(q)
        out = []
        for rec in candidates:
            title = str(rec.get("title") or "").lower()
            body = str(rec.get("content") or "").lower()
            score = 0
            title_hit = 0
            for token in tokens:
                if not token:
                    continue
                if token in title:
                    title_hit += 1
                    score += 3
                score += body.count(token)
            if q and score <= 0:
                continue
            item = dict(rec)
            item["score"] = score
            item["_title_hit"] = title_hit
            out.append(item)
        out.sort(key=lambda x: (-int(x.get("_title_hit") or 0),
                                -int(x.get("score") or 0),
                                -float(x.get("updated_at") or 0)))
        for item in out:
            item.pop("_title_hit", None)
        return out[:max(0, int(top_k or 0))] if top_k else out

    def get_chunk(self, chunk_id: str, tenant_id: str = "") -> dict | None:
        sql = "SELECT * FROM harness_knowledge_chunk WHERE id=%s"
        args: list = [str(chunk_id)]
        if tenant_id:
            sql += " AND tenant_id=%s"
            args.append(tenant_id)
        row = self.db.fetchone(sql, args)
        if row is None or row.get("review_status") == "archived":
            return None
        return row

    def close(self) -> None:
        self.db.close()
