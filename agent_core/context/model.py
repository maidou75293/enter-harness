"""统一记忆模型：五类记忆 + 派生权威 + 块契约。

对齐 CoALA（working / episodic / semantic / procedural）+ profile，
见 docs/统一规范-知识记忆上下文-v1.md（v1.3）。

约定：
- 机器枚举一律小写，显示名在文档里给中文；
- `authority` 不落库，由 `kind` 派生（derive_authority）；
- 只追加 + supersede 失效，不物理删、不做多版本检索。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field

# ---------------------------------------------------------------- 枚举

# 块/上下文层可出现的 kind（会话层也有 working / episodic）
KINDS = ("working", "episodic", "semantic", "procedural", "profile")

# 长期记忆记录只落三类：工作记忆、情景记忆存会话表，不使用记录结构
RECORD_KINDS = ("semantic", "procedural", "profile")

SCOPES = ("platform", "project", "user", "session")

STATUSES = ("active", "superseded", "archived", "deleted")

SUBTYPES = {
    "semantic": ("decision", "fact"),
    "procedural": ("policy", "contract", "sop", "skill"),
    "profile": ("preference",),
}

# 派生权威：不落库，只用于排序与冲突裁决
AUTHORITY_BY_KIND = {
    "procedural": "MUST",
    "semantic": "IS",
    "profile": "WANT",
    "episodic": "NOW",
    "working": "NOW",
}

# 注入次序（数字越小越靠前）：不是分类学的顺序，是拼装顺序。
# procedural(必须) → semantic(是) → profile(想) → episodic/working(对话)
KIND_ORDER = {"procedural": 0, "semantic": 1, "profile": 2,
              "episodic": 3, "working": 4}
SCOPE_ORDER = {s: i for i, s in enumerate(SCOPES)}

# D1：跨 scope 的优先级按 kind 分开——越具体越优先（规则类例外，平台压项目）
SCOPE_ORDER_BY_KIND = {
    "procedural": {"platform": 0, "project": 1, "user": 2, "session": 3},
    "semantic": {"project": 0, "platform": 1, "user": 2, "session": 3},
    "profile": {"user": 0, "project": 1, "platform": 2, "session": 3},
    "episodic": {"session": 0, "project": 1, "user": 2, "platform": 3},
    "working": {"session": 0, "project": 1, "user": 2, "platform": 3},
}


def scope_rank(kind: str, scope: str) -> int:
    order = SCOPE_ORDER_BY_KIND.get(normalize_kind(kind)) or SCOPE_ORDER
    return order.get(normalize_scope(scope), 99)


def derive_authority(kind: str) -> str:
    """kind → 权威档；未知 kind 退回 IS，不抛异常（脏数据不拖垮拼装）。"""
    return AUTHORITY_BY_KIND.get(str(kind or ""), "IS")


def content_hash(text: str) -> str:
    """块/记忆内容的稳定指纹；缓存键用它，不用 version。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def normalize_kind(kind: str) -> str:
    k = str(kind or "").strip().lower()
    return k if k in KINDS else "semantic"


def normalize_scope(scope: str) -> str:
    s = str(scope or "").strip().lower()
    return s if s in SCOPES else "project"


def normalize_status(status: str) -> str:
    s = str(status or "").strip().lower()
    return s if s in STATUSES else "active"


# ---------------------------------------------------------------- 块


@dataclass
class BlockMeta:
    """块的元数据：必须便宜（不含正文），用于每轮版本比对。"""

    id: str
    kind: str = "semantic"
    scope: str = "project"
    scope_id: str = ""
    tenant_id: str = ""
    priority: int = 500
    max_chars: int = 0
    cacheable: bool = False
    version: int = 1
    content_hash: str = ""
    owner: str = "platform"
    source: str = ""

    def __post_init__(self):
        self.kind = normalize_kind(self.kind)
        self.scope = normalize_scope(self.scope)

    @property
    def authority(self) -> str:
        return derive_authority(self.kind)

    @property
    def sort_key(self) -> tuple:
        return (KIND_ORDER.get(self.kind, 99),
                SCOPE_ORDER.get(self.scope, 99),
                int(self.priority or 0),
                str(self.id))


@dataclass
class Block:
    """块正文 + 元数据。正文与 content_hash 必须自洽。"""

    meta: BlockMeta
    text: str = ""

    def __post_init__(self):
        if not self.meta.content_hash and self.text:
            self.meta.content_hash = content_hash(self.text)

    @property
    def id(self) -> str:
        return self.meta.id


# ---------------------------------------------------------------- 记忆记录


@dataclass
class MemoryRecord:
    """一条记忆；字段与规范 v1.3 §3 一致。"""

    id: str = ""
    tenant_id: str = ""
    kind: str = "semantic"
    subtype: str = ""
    scope: str = "project"
    scope_id: str = ""
    agent_id: str = ""
    session_id: str = ""
    key: str = ""
    content: str = ""
    summary: str = ""
    outcome: str = ""            # 仅 episodic：结果 / 失败原因
    condition: dict = field(default_factory=dict)   # 结构化条件，如 {"env": "DEV"}
    status_reason: str = ""      # user_retracted / expired / superseded / policy
    provenance: list = field(default_factory=list)   # 多次证实的来源列表
    source: str = ""
    importance: float = 0.5
    confidence: float = 0.6
    status: str = "active"
    superseded_by: str = ""
    expires_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0
    content_hash: str = ""
    version: int = 1
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.kind = normalize_kind(self.kind)
        self.scope = normalize_scope(self.scope)
        self.status = normalize_status(self.status)
        self.subtype = str(self.subtype or "").strip().lower()
        allowed = SUBTYPES.get(self.kind)
        # x- 前缀是产品自有的自由标签：不改变 kind 派生的权威，也不参与排序
        if self.subtype.startswith("x-"):
            pass
        elif allowed and self.subtype and self.subtype not in allowed:
            self.subtype = ""
        if not self.content_hash:
            # 指纹算「实际注入的文本」：有摘要用摘要，否则用正文。
            # summary 变了、content 没变，也要让缓存失效。
            self.content_hash = content_hash(self.summary or self.content)
        if not isinstance(self.condition, dict):
            self.condition = {}
        if not isinstance(self.provenance, list):
            self.provenance = []
        self.updated_at = self.updated_at or self.created_at or time.time()
        self.created_at = self.created_at or self.updated_at

    @property
    def authority(self) -> str:
        return derive_authority(self.kind)

    def to_dict(self) -> dict:
        return asdict(self)


class WriteResult(dict):
    """写入四态：append / supersede / noop / invalid。

    用 dict 子类是为了兼容旧调用方（`r["action"]`），同时给端口一个明确类型。
    """

    def __init__(self, action: str = "append", id: str = "",
                 replaced_id: str = "", reason: str = "", ok: bool = True):
        super().__init__(ok=ok, action=action, id=id,
                         replaced_id=replaced_id, reason=reason)

    @property
    def action(self) -> str:
        return str(self["action"])

    @property
    def id(self) -> str:
        return str(self["id"])

    @property
    def replaced_id(self) -> str:
        return str(self["replaced_id"])

    @property
    def reason(self) -> str:
        return str(self["reason"])


def validate_record(rec) -> str:
    """写时校验：返回错误原因，空串表示合法（由 MemoryPort 在写前调用）。"""
    if not isinstance(rec, MemoryRecord):
        return "not_memory_record"
    if not str(rec.tenant_id or "").strip():
        return "tenant_id_required"
    if rec.kind not in RECORD_KINDS:
        return f"kind_not_persisted:{rec.kind}"
    if rec.scope not in SCOPES:
        return f"scope_unknown:{rec.scope}"
    if rec.scope != "platform" and not str(rec.scope_id or "").strip():
        return "scope_id_required"
    if not str(rec.content or "").strip():
        return "content_required"
    if not str(rec.source or "").strip():
        return "source_required"
    if rec.kind in ("semantic", "procedural") and not str(rec.key or "").strip():
        return "key_required"
    if rec.status != "active":
        return f"status_not_writable:{rec.status}"
    return ""


def is_active(rec, now: float | None = None) -> bool:
    """active 且未过期；过期不注入、不物理删。"""
    status = normalize_status(getattr(rec, "status", "active"))
    if status != "active":
        return False
    expires_at = getattr(rec, "expires_at", None)
    if expires_at is not None and float(expires_at) > 0:
        return float(expires_at) > (now if now is not None else time.time())
    return True


def is_visible(rec, identity: dict) -> bool:
    """按 namespace 判可见性；session 才按 session_id 隔离。

    低配产品可以不传某些字段：缺字段时该维度不参与过滤（不填假数据）。
    """
    identity = identity or {}
    tenant_id = str(identity.get("tenant_id") or "")
    rec_tenant = str(getattr(rec, "tenant_id", "") or "")
    if tenant_id and rec_tenant and rec_tenant != tenant_id:
        return False
    scope = normalize_scope(getattr(rec, "scope", "project"))
    scope_id = str(getattr(rec, "scope_id", "") or "")
    if scope == "platform":
        return True
    if scope == "project":
        pid = str(identity.get("project_id") or "")
        projects = identity.get("projects") or ()
        if not pid and not projects:
            return True
        return scope_id == pid or scope_id in {str(x) for x in projects}
    if scope == "user":
        uid = str(identity.get("user_id") or "")
        return (not uid) or scope_id == uid
    if scope == "session":
        sid = str(identity.get("session_id") or "")
        rec_sid = str(getattr(rec, "session_id", "") or "")
        return (not sid) or (not rec_sid) or rec_sid == sid
    return True


def select_records(records, identity: dict, now: float | None = None,
                   include_shadowed: bool = False):
    """过滤 + 排序 + 压制。

    次序：kind → 该 kind 的 scope 次序 → importance → updated_at。
    压制（D2）：同 kind、同 key、不同 scope 只保留优先级最高的一条，
    其余进 shadowed（保留可审计，但不注入）。
    """
    out = [r for r in (records or [])
           if is_active(r, now) and is_visible(r, identity)]
    out.sort(key=lambda r: (KIND_ORDER.get(normalize_kind(r.kind), 99),
                            scope_rank(r.kind, r.scope),
                            -float(getattr(r, "importance", 0) or 0),
                            -float(getattr(r, "updated_at", 0) or 0)))
    kept, shadowed, seen = [], [], {}
    for r in out:
        key = str(getattr(r, "key", "") or "")
        cond = json.dumps(getattr(r, "condition", {}) or {}, sort_keys=True,
                          ensure_ascii=False, default=str)
        # R1 含 agent_id：共享记忆 agent_id=''，不同智能体各有各的 key 空间
        aid = str(getattr(r, "agent_id", "") or "")
        bucket = (normalize_kind(r.kind), aid, key, cond)
        if key and bucket in seen:
            shadowed.append(r)
            continue
        if key:
            seen[bucket] = r
        kept.append(r)
    return (kept, shadowed) if include_shadowed else kept
