"""本地端口工厂：SQLite / Markdown / PostgreSQL 三套后端。

命名规范：`{Backend}{Concept}Port`。

- SQLite（默认）：记忆、知识、状态、审计四件套全落 SQLite；
- Markdown：记忆与知识落人可读写的 Markdown，状态与审计仍落 SQLite；
- PostgreSQL：四件套全落 PG（`harness_*` 表），需要 `psycopg`，
  DSN 可传参或用 `HARNESS_PG_DSN` / `DATABASE_URL` / `PG*` 环境变量。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..context.store import SQLiteMemoryStore
from .audit import SQLiteAuditPort
from .knowledge import SQLiteKnowledgePort
from .markdown_knowledge import MarkdownKnowledgePort
from .markdown_memory import MarkdownMemoryPort
from .memory import StoreMemoryPort
from .postgres_audit import PostgresAuditPort
from .postgres_knowledge import PostgresKnowledgePort
from .postgres_memory import PostgresMemoryPort
from .postgres_state import PostgresStatePort
from .state import SQLiteStatePort

BACKENDS = ("sqlite", "markdown", "postgres")


@dataclass
class LocalPorts:
    backend: str
    memory: object
    knowledge: object
    state: object
    audit: object
    dsn: str = ""

    def close(self) -> None:
        for port in (self.memory, self.knowledge, self.state, self.audit):
            close = getattr(port, "close", None)
            if callable(close):
                close()


def build_local_ports(root: str | Path, backend: str = "sqlite",
                      dsn: str = "") -> LocalPorts:
    """按后端创建本地端口（记忆 / 知识 / 状态 / 审计）。

    - sqlite：root 下建 memory.db / knowledge.db / state.db / audit.db；
    - markdown：root 下建 memory/ 与 knowledge/，状态与审计仍落 state.db / audit.db；
    - postgres：使用 dsn（缺省读 HARNESS_PG_DSN / DATABASE_URL / PG*），
      root 只作为占位、不建目录；构造时自动 CREATE TABLE IF NOT EXISTS。
    """
    backend = str(backend or "sqlite").strip().lower()
    if backend not in BACKENDS:
        raise ValueError(f"未知本地后端：{backend}（可选 {', '.join(BACKENDS)}）")
    base = Path(root)
    if backend != "postgres":
        base.mkdir(parents=True, exist_ok=True)

    if backend == "markdown":
        memory = MarkdownMemoryPort(base / "memory")
        knowledge = MarkdownKnowledgePort(base / "knowledge")
        state = SQLiteStatePort(str(base / "state.db"))
        audit = SQLiteAuditPort(str(base / "audit.db"))
    elif backend == "postgres":
        memory = PostgresMemoryPort(dsn)
        knowledge = PostgresKnowledgePort(dsn)
        state = PostgresStatePort(dsn)
        audit = PostgresAuditPort(dsn)
    else:
        memory = StoreMemoryPort(SQLiteMemoryStore(str(base / "memory.db")))
        knowledge = SQLiteKnowledgePort(str(base / "knowledge.db"))
        state = SQLiteStatePort(str(base / "state.db"))
        audit = SQLiteAuditPort(str(base / "audit.db"))

    return LocalPorts(
        backend=backend, memory=memory, knowledge=knowledge,
        state=state, audit=audit, dsn=dsn,
    )
