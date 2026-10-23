"""上下文管理：窗口预算 / 自动压缩 / 记忆注入 / 统一拼装流水线。"""
from .budget import fit_window, trim_messages
from .manager import ContextManager
from .metrics import ContextMetrics, MetricsRegistry, summarize
from .model import (KINDS, RECORD_KINDS, SCOPES, STATUSES, SUBTYPES, Block,
                    BlockMeta, MemoryRecord, WriteResult, content_hash,
                    derive_authority, is_active, is_visible, select_records,
                    validate_record)
from .pipeline import BlockCache, assemble, estimate_tokens
from .ports import (AccessPolicyPort, ContextPort, IdentityPort,
                    KnowledgePort, MemoryPort)
from .store import SQLiteMemoryStore

__all__ = [
    "ContextManager", "fit_window", "trim_messages",
    "assemble", "BlockCache", "estimate_tokens",
    "ContextMetrics", "MetricsRegistry", "summarize",
    "Block", "BlockMeta", "MemoryRecord", "WriteResult",
    "KINDS", "RECORD_KINDS", "SCOPES", "STATUSES", "SUBTYPES",
    "content_hash", "derive_authority", "is_active", "is_visible",
    "select_records", "validate_record",
    "IdentityPort", "ContextPort", "MemoryPort", "KnowledgePort",
    "AccessPolicyPort", "SQLiteMemoryStore",
]
