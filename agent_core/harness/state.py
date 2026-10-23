"""默认状态端口：SQLite 保存运行状态与检查点。

与 LangGraph checkpointer 的关系：
- 默认实现不依赖 langgraph，保证只有基础依赖时也能跑；
- 宿主可把 LangGraph checkpointer 包一层适配器，实现同一契约。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Sequence

from .types import Checkpoint

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_state(
  thread_id TEXT PRIMARY KEY,
  state TEXT NOT NULL DEFAULT '{}',
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run_checkpoint(
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  node TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '{}',
  parent_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_thread
  ON run_checkpoint(thread_id, created_at);
"""


class SQLiteStatePort:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 基本读写
    def load(self, thread_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT state FROM run_state WHERE thread_id=?", (thread_id,)).fetchone()
        return self._loads(row["state"]) if row else None

    def save(self, thread_id: str, state: dict) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO run_state(thread_id, state, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET state=excluded.state, "
                "updated_at=excluded.updated_at",
                (thread_id, self._dumps(state), time.time()))

    # ------------------------------------------------------------ 检查点
    def checkpoint(self, thread_id: str, node: str,
                   state: dict | None = None) -> str:
        state = state if state is not None else (self.load(thread_id) or {})
        row = self.conn.execute(
            "SELECT id FROM run_checkpoint WHERE thread_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (thread_id,)).fetchone()
        ck = Checkpoint(thread_id=thread_id, node=node, state=state,
                        parent_id=str(row["id"]) if row else "")
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO run_checkpoint(id, thread_id, node, state, parent_id, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (ck.id, ck.thread_id, ck.node, self._dumps(ck.state),
                 ck.parent_id, ck.created_at))
        return ck.id

    def resume(self, thread_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT state FROM run_checkpoint WHERE thread_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (thread_id,)).fetchone()
        if row is not None:
            return self._loads(row["state"])
        return self.load(thread_id)

    def list_checkpoints(self, thread_id: str) -> Sequence[dict]:
        rows = self.conn.execute(
            "SELECT * FROM run_checkpoint WHERE thread_id=? "
            "ORDER BY created_at ASC, rowid ASC", (thread_id,)).fetchall()
        return [self._row(r, include_state=False) for r in rows]

    def prune(self, before: float | None = None,
              retention_days: float = 30) -> int:
        """删除过期状态与检查点，返回删除行数。

        ``before`` 是 Unix 时间戳；省略时按 ``now - retention_days * 86400``。
        这是可选能力，不在 StatePort 协议里，避免破坏宿主自定义实现。
        """
        cutoff = (float(before) if before is not None
                  else time.time() - max(0.0, float(retention_days or 0)) * 86400)
        with self._lock, self.conn:
            ck = self.conn.execute(
                "DELETE FROM run_checkpoint WHERE created_at < ?", (cutoff,)).rowcount
            st = self.conn.execute(
                "DELETE FROM run_state WHERE updated_at < ?", (cutoff,)).rowcount
        return int(ck or 0) + int(st or 0)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _dumps(state: dict) -> str:
        return json.dumps(state or {}, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _loads(text: str) -> dict:
        try:
            value = json.loads(text or "{}")
        except Exception:  # noqa: BLE001
            return {}
        return value if isinstance(value, dict) else {"value": value}

    @staticmethod
    def _row(row, include_state: bool = True) -> dict:
        ck = Checkpoint(
            id=str(row["id"]), thread_id=str(row["thread_id"]),
            node=str(row["node"]), state=SQLiteStatePort._loads(row["state"]),
            parent_id=str(row["parent_id"]), created_at=float(row["created_at"]))
        return ck.to_dict(include_state=include_state)
