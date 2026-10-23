"""PostgreSQL 状态：运行状态 + 检查点。"""
from __future__ import annotations

import json
import time
import uuid
from typing import Sequence

from .pg import PgStore

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS harness_run_state(
      thread_id TEXT PRIMARY KEY,
      state JSONB NOT NULL DEFAULT '{}'::jsonb,
      updated_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS harness_run_checkpoint(
      id TEXT PRIMARY KEY,
      thread_id TEXT NOT NULL,
      node TEXT NOT NULL DEFAULT '',
      state JSONB NOT NULL DEFAULT '{}'::jsonb,
      parent_id TEXT NOT NULL DEFAULT '',
      created_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_harness_checkpoint_thread
       ON harness_run_checkpoint(thread_id, created_at)""",
]


class PostgresStatePort:
    def __init__(self, dsn: str = "", *, conn=None):
        self.db = PgStore(dsn, conn=conn)
        self.db.setup(SCHEMA)

    def load(self, thread_id: str) -> dict | None:
        row = self.db.fetchone(
            "SELECT state FROM harness_run_state WHERE thread_id=%s",
            (str(thread_id),))
        return dict(row["state"] or {}) if row else None

    def save(self, thread_id: str, state: dict) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO harness_run_state(thread_id, state, updated_at) "
                "VALUES (%s,%s::jsonb,%s) ON CONFLICT(thread_id) DO UPDATE "
                "SET state=excluded.state, updated_at=excluded.updated_at",
                (str(thread_id), json.dumps(state or {}, ensure_ascii=False,
                                            default=str), time.time()))

    def checkpoint(self, thread_id: str, node: str,
                   state: dict | None = None) -> str:
        state = state if state is not None else (self.load(thread_id) or {})
        latest = self.db.fetchone(
            "SELECT id FROM harness_run_checkpoint WHERE thread_id=%s "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (str(thread_id),))
        cid = "ck_" + uuid.uuid4().hex[:12]
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO harness_run_checkpoint(id, thread_id, node, state, "
                "parent_id, created_at) VALUES (%s,%s,%s,%s::jsonb,%s,%s)",
                (cid, str(thread_id), str(node),
                 json.dumps(state or {}, ensure_ascii=False, default=str),
                 str((latest or {}).get("id") or ""), time.time()))
        return cid

    def resume(self, thread_id: str) -> dict | None:
        row = self.db.fetchone(
            "SELECT state FROM harness_run_checkpoint WHERE thread_id=%s "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (str(thread_id),))
        if row is not None:
            return dict(row["state"] or {})
        return self.load(thread_id)

    def list_checkpoints(self, thread_id: str) -> Sequence[dict]:
        return self.db.fetchall(
            "SELECT id, thread_id, node, parent_id, created_at "
            "FROM harness_run_checkpoint WHERE thread_id=%s "
            "ORDER BY created_at ASC, id ASC", (str(thread_id),))

    def prune(self, before: float | None = None,
              retention_days: float = 30) -> int:
        """删除过期状态与检查点，返回删除行数。

        ``before`` 是 Unix 时间戳；省略时按 ``now - retention_days * 86400``。
        这是可选能力，不在 StatePort 协议里，避免破坏宿主自定义实现。
        """
        cutoff = (float(before) if before is not None
                  else time.time() - max(0.0, float(retention_days or 0)) * 86400)
        with self.db.transaction() as conn:
            ck = conn.execute(
                "DELETE FROM harness_run_checkpoint WHERE created_at < %s",
                (cutoff,)).rowcount
            st = conn.execute(
                "DELETE FROM harness_run_state WHERE updated_at < %s",
                (cutoff,)).rowcount
        return int(ck or 0) + int(st or 0)

    def close(self) -> None:
        self.db.close()
