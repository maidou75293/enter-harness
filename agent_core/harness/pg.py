"""PostgreSQL 公共连接层：延迟导入 psycopg，DB-API 行转 dict。

没有装驱动时不拖垮 `import agent_core.harness`；只有真正使用 Postgres 后端
才抛错，并给出安装命令。
"""
from __future__ import annotations

import contextlib
import os
import threading
from typing import Any, Iterator
from urllib.parse import quote


def dsn_from_env() -> str:
    dsn = (os.environ.get("HARNESS_PG_DSN")
           or os.environ.get("DATABASE_URL") or "")
    if dsn:
        return dsn
    if os.environ.get("PGHOST"):
        return (
            "postgresql://"
            f"{quote(os.environ.get('PGUSER', 'harness'), safe='')}:"
            f"{quote(os.environ.get('PGPASSWORD', ''), safe='')}@"
            f"{os.environ.get('PGHOST')}:{os.environ.get('PGPORT', '5432')}/"
            f"{os.environ.get('PGDATABASE', 'harness')}"
        )
    return ""


def connect(dsn: str = ""):
    try:
        import psycopg  # type: ignore
    except ModuleNotFoundError as e:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "PostgreSQL 后端需要 psycopg：pip install 'agent-core[postgres]'"
        ) from e
    target = dsn or dsn_from_env()
    if not target:
        raise ValueError("缺少 PostgreSQL DSN（参数、HARNESS_PG_DSN 或 DATABASE_URL）")
    return psycopg.connect(target, autocommit=False, connect_timeout=5)


def rows(cursor) -> list[dict]:
    cols = [d[0] for d in (cursor.description or ())]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


class PgStore:
    """一个连接 + 一把锁；端口实例内部串行，跨实例由数据库保证。"""

    def __init__(self, dsn: str = "", conn: Any = None):
        self.conn = conn if conn is not None else connect(dsn)
        self._lock = threading.RLock()

    def setup(self, statements) -> None:
        with self._lock:
            try:
                for sql in statements:
                    self.conn.execute(sql)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def fetchall(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return rows(self.conn.execute(sql, params))

    def fetchone(self, sql: str, params=()) -> dict | None:
        with self._lock:
            cur = self.conn.execute(sql, params)
            data = rows(cur)
            return data[0] if data else None

    def execute(self, sql: str, params=()):
        with self._lock:
            return self.conn.execute(sql, params)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._lock:
            try:
                yield self.conn
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
