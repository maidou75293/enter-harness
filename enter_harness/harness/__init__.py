"""enter-harness 内核：7 个模块 / 10 个治理端口。

定位：模型之外的运行时。LangGraph 负责「怎么跑」，
本包负责「给模型看什么、记住什么、谁能看、留什么证据」。
"""
from .action import (InMemoryApprovalQueue, SQLiteApprovalQueue,
                     ToolActionPort)
from .audit import SQLiteAuditPort, SQLiteAuditSink
from .constants import (ACTION_STATUSES, APPROVAL_STATUSES, AUDIT_DECISIONS,
                        REVIEW_STATUSES, RUN_STATUSES, WRITE_ACTIONS)
from .context import MemoryContextPort
from .factory import BACKENDS, LocalPorts, build_local_ports
from .identity import StaticIdentityPort
from .ingest import DefaultIngestPort
from .knowledge import SQLiteKnowledgePort
from .markdown_knowledge import MarkdownKnowledgePort
from .markdown_memory import MarkdownMemoryPort
from .memory import StoreMemoryPort
from .migrate import dump_ports, load_ports, migrate_ports
from .postgres_audit import PostgresAuditPort
from .postgres_knowledge import PostgresKnowledgePort
from .postgres_memory import PostgresMemoryPort
from .postgres_state import PostgresStatePort
from .ports import (CORE_PORT_COUNT, CORE_PORT_NAMES, MODULE_PORTS,
                    PORT_PROTOCOLS, AccessPolicyPort, ActionPort, AuditPort,
                    AuditSink, ContextPort, IdentityPort, IngestPort,
                    KnowledgePort, MemoryPort, ModelPort, StatePort, VerifyPort)
from .state import SQLiteStatePort
from .types import (ActionResult, ApprovalTicket, AuditEvent, Checkpoint,
                    RawInput, Verdict)
from .verify import HANDOFF_MARKERS, RuleVerifyPort
from .wiring import PortSet
from .write import check_record, write_record

__all__ = [
    # ports
    "IdentityPort", "ContextPort", "MemoryPort", "KnowledgePort",
    "AccessPolicyPort", "IngestPort", "ActionPort", "StatePort",
    "VerifyPort", "AuditPort", "AuditSink", "ModelPort",
    "PORT_PROTOCOLS", "MODULE_PORTS", "CORE_PORT_NAMES", "CORE_PORT_COUNT",
    "PortSet",
    # types / constants
    "RawInput", "ActionResult", "ApprovalTicket", "Verdict", "AuditEvent",
    "Checkpoint", "ACTION_STATUSES", "RUN_STATUSES", "WRITE_ACTIONS",
    "APPROVAL_STATUSES", "REVIEW_STATUSES", "AUDIT_DECISIONS",
    # defaults
    "DefaultIngestPort", "StoreMemoryPort", "MemoryContextPort",
    "SQLiteKnowledgePort", "MarkdownMemoryPort", "MarkdownKnowledgePort",
    "PostgresMemoryPort", "PostgresKnowledgePort", "PostgresStatePort",
    "PostgresAuditPort", "LocalPorts", "build_local_ports", "BACKENDS",
    "StaticIdentityPort", "ToolActionPort",
    "InMemoryApprovalQueue", "SQLiteApprovalQueue", "SQLiteStatePort",
    "SQLiteAuditPort", "SQLiteAuditSink", "RuleVerifyPort", "HANDOFF_MARKERS",
    "check_record", "write_record",
    # migration
    "dump_ports", "load_ports", "migrate_ports",
]
