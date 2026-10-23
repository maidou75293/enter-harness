"""默认动作端口：工具清单 / 调用 / 审批 / 恢复。

底层复用 ToolRegistry（注册、白名单、策略、沙箱、输出治理），
本模块只补三件事：统一结果、审批单持久化、恢复时跳过策略复判。
"""
from __future__ import annotations

import inspect
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Sequence

from .types import ActionResult, ApprovalTicket, AuditEvent

APPROVED = {"approve", "approved", "allow", "ok", "yes", "true", "通过", "同意"}
REJECTED = {"reject", "rejected", "deny", "denied", "no", "false", "拒绝", "驳回"}


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, (list, tuple, set)):
        return {str(x) for x in value if str(x)}
    return set()


def _tool_meta(tool: dict) -> dict:
    meta = tool.get("metadata")
    return meta if isinstance(meta, dict) else {}


_TOOL_FIELDS = ("name", "description", "parameters", "danger", "source", "tags",
                "metadata", "enabled", "parallel", "scope", "tenants", "roles",
                "risk", "side_effect", "approval")


def _field(verdict: Any, name: str, default: Any = "") -> Any:
    """策略返回值可能是 dict 或对象；统一取字段。"""
    if isinstance(verdict, dict):
        return verdict.get(name, default)
    return getattr(verdict, name, default)


def _decision_of(verdict: Any) -> str:
    """策略返回值统一成 allow / ask / deny；未知值按 ask（默认不放行）。"""
    if verdict is None:
        return "allow"
    raw = (verdict.get("decision") if isinstance(verdict, dict)
           else getattr(verdict, "decision", "allow"))
    text = str(raw or "allow").strip().lower()
    if text == "needs_approval":
        return "ask"
    return text if text in ("allow", "ask", "deny") else "ask"


def _tool_dict(tool: Any) -> dict | None:
    """把 dict / FunctionTool / 任意带同名字段的对象统一成 dict。

    目录返回值必须是 dict（ActionPort 契约）；兼容
    `enter_harness.tooling.ToolRegistry.list_tools()` 返回的 FunctionTool。
    """
    if isinstance(tool, dict):
        return tool
    if tool is None:
        return None
    out: dict[str, Any] = {}
    for field in _TOOL_FIELDS:
        if hasattr(tool, field):
            out[field] = getattr(tool, field)
    if "parallel" not in out and hasattr(tool, "parallel_safe"):
        out["parallel"] = bool(getattr(tool, "parallel_safe"))
    if not out.get("name"):
        return None
    out.setdefault("description", "")
    out.setdefault("parameters", {"type": "object", "properties": {}})
    out.setdefault("danger", False)
    out.setdefault("tags", [])
    out.setdefault("enabled", True)
    out.setdefault("scope", "")
    return out


def _visible(tool: dict, identity: dict, scope: str) -> bool:
    """工具可见性：scope + 租户 + 角色。声明了限制但身份不满足时默认拒绝。"""
    tags = _as_set(tool.get("tags"))
    tool_scope = str(tool.get("scope") or _tool_meta(tool).get("scope") or "")
    if scope:
        # 兼容旧语义：请求 scope 时，工具必须显式声明该 scope 或带同名 tag
        if tool_scope:
            if tool_scope != scope and scope not in tags:
                return False
        elif scope not in tags:
            return False

    meta = _tool_meta(tool)
    tenants = _as_set(tool.get("tenants") or meta.get("tenants")
                      or meta.get("tenant_ids"))
    if tenants:
        tenant = str(identity.get("tenant_id") or identity.get("tenant") or "")
        if tenant not in tenants:
            return False

    roles = _as_set(tool.get("roles") or meta.get("roles"))
    if roles:
        have = _as_set(identity.get("roles") or identity.get("role_ids"))
        if not (have & roles):
            return False
    return True


class InMemoryApprovalQueue:
    """单进程默认队列；多副本部署请换 SQLite 或外部审批系统。"""

    def __init__(self):
        self._items: dict[str, ApprovalTicket] = {}

    def add(self, ticket: ApprovalTicket) -> ApprovalTicket:
        self._items[ticket.id] = ticket
        return ticket

    def get(self, ticket_id: str) -> ApprovalTicket | None:
        return self._items.get(ticket_id)

    def update(self, ticket: ApprovalTicket) -> ApprovalTicket:
        self._items[ticket.id] = ticket
        return ticket

    def list(self, tenant_id: str = "", status: str = "") -> list[ApprovalTicket]:
        return [t for t in self._items.values()
                if (not tenant_id or t.tenant_id == tenant_id)
                and (not status or t.status == status)]


class SQLiteApprovalQueue:
    """持久审批队列：服务重启后 pending 单还在。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS approval(
      id TEXT PRIMARY KEY,
      tenant_id TEXT NOT NULL DEFAULT '',
      actor TEXT NOT NULL DEFAULT '',
      tool TEXT NOT NULL DEFAULT '',
      payload TEXT NOT NULL DEFAULT '{}',
      scope TEXT NOT NULL DEFAULT 'user',
      status TEXT NOT NULL DEFAULT 'pending',
      reason TEXT NOT NULL DEFAULT '',
      created_at REAL NOT NULL,
      decided_at REAL NOT NULL DEFAULT 0,
      decided_by TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_approval_status ON approval(tenant_id, status);
    """

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    def add(self, ticket: ApprovalTicket) -> ApprovalTicket:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO approval(id, tenant_id, actor, tool, payload, scope, "
                "status, reason, created_at, decided_at, decided_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ticket.id, ticket.tenant_id, ticket.actor, ticket.tool,
                 json.dumps(ticket.payload, ensure_ascii=False, default=str),
                 ticket.scope, ticket.status, ticket.reason, ticket.created_at,
                 ticket.decided_at, ticket.decided_by))
        return ticket

    def get(self, ticket_id: str) -> ApprovalTicket | None:
        row = self.conn.execute(
            "SELECT * FROM approval WHERE id=?", (ticket_id,)).fetchone()
        return self._row(row) if row else None

    def update(self, ticket: ApprovalTicket) -> ApprovalTicket:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE approval SET status=?, reason=?, decided_at=?, decided_by=? "
                "WHERE id=?",
                (ticket.status, ticket.reason, ticket.decided_at,
                 ticket.decided_by, ticket.id))
        return ticket

    def list(self, tenant_id: str = "", status: str = "") -> list[ApprovalTicket]:
        sql = ["SELECT * FROM approval WHERE 1=1"]
        args: list = []
        if tenant_id:
            sql.append("AND tenant_id=?")
            args.append(tenant_id)
        if status:
            sql.append("AND status=?")
            args.append(status)
        sql.append("ORDER BY created_at ASC")
        return [self._row(r) for r in self.conn.execute(" ".join(sql), args)]

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _row(row) -> ApprovalTicket:
        d = dict(row)
        try:
            payload = json.loads(d.get("payload") or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        return ApprovalTicket(
            id=d["id"], tenant_id=d["tenant_id"], actor=d["actor"],
            tool=d["tool"], payload=payload if isinstance(payload, dict) else {},
            scope=d["scope"], status=d["status"], reason=d["reason"],
            created_at=float(d["created_at"] or 0),
            decided_at=float(d.get("decided_at") or 0),
            decided_by=d.get("decided_by") or "")


class ToolActionPort:
    """把 ToolRegistry 适配成 ActionPort。

    可选 `policy`（`ToolPolicyPort`）：传入后 `call` 会先做 allow/ask/deny 判定，
    `ask` 返回 `status="needs_approval"` 并带 `approval_id`，由宿主落单与恢复。
    """

    def __init__(self, registry, *, queue=None, audit=None, policy=None):
        self.registry = registry
        self.queue = queue or InMemoryApprovalQueue()
        self.audit = audit
        self.policy = policy

    # ------------------------------------------------------------ 清单
    def list_tools(self, identity: dict, scope: str = "") -> Sequence[dict]:
        ident = dict(identity or {})
        out = []
        for raw in self.registry.list_tools():
            tool = _tool_dict(raw)
            if tool is not None and _visible(tool, ident, scope):
                out.append(tool)
        return out

    def validate(self) -> list[str]:
        """装配期校验：工具名、schema、可见性字段；返回问题列表。"""
        errs: list[str] = []
        validator = getattr(self.registry, "validate", None)
        if callable(validator):
            errs.extend(str(x) for x in (validator() or []))
        try:
            tools = list(self.registry.list_tools())
        except Exception as e:  # noqa: BLE001
            return errs + [f"list_tools_failed:{type(e).__name__}:{e}"]

        seen: set[str] = set()
        for raw in tools:
            tool = _tool_dict(raw)
            if tool is None:
                errs.append("tool_not_object")
                continue
            name = str(tool.get("name") or "")
            if not name:
                errs.append("tool_name_required")
            elif name in seen:
                errs.append(f"duplicate_tool:{name}")
            seen.add(name)
            params = tool.get("parameters")
            if params is not None and not isinstance(params, dict):
                errs.append(f"{name}:parameters_not_object")
            danger = tool.get("danger")
            if danger is not None and not isinstance(danger, bool):
                errs.append(f"{name}:danger_not_bool")
            for field in ("tenants", "roles"):
                value = tool.get(field)
                if value is None:
                    value = _tool_meta(tool).get(field)
                if value is not None and not isinstance(value, (list, tuple, set)):
                    errs.append(f"{name}:{field}_not_list")
        return errs

    # ------------------------------------------------------------ 调用
    def _visibility_error(self, name: str, identity: dict) -> str:
        """按目录元数据判可见；未知工具交给底层注册表报错。"""
        try:
            tools = list(self.registry.list_tools())
        except Exception:  # noqa: BLE001
            return ""
        for raw in tools:
            tool = _tool_dict(raw)
            if tool is None or tool.get("name") != name:
                continue
            if _visible(tool, identity, ""):
                return ""
            return f"工具不可见：{name}"
        return ""

    async def call(self, identity: dict, name: str, args: dict,
                   ctx: Any = None) -> ActionResult:
        ident = identity or {}
        context = self._ctx(ident, ctx)
        t0 = time.time()
        # 审批恢复已经过一次授权，不再按当前身份重判可见性
        if not context.get("approved_override"):
            denied = self._visibility_error(name, ident)
            if denied:
                self._audit(ident, f"tool:{name}", name, "denied", denied,
                            str(context.get("run") or ""))
                return ActionResult(
                    ok=False, status="denied", error=denied,
                    meta={"tool": name, "args": args, "ctx": ctx})
            if self.policy is not None:
                verdict = self.policy.check(ctx, {"tool": name, "args": args})
                decision = _decision_of(verdict)
                reason = str(_field(verdict, "reason") or "")
                if decision == "deny":
                    self._audit(ident, f"tool:{name}", name, "denied", reason,
                                str(context.get("run") or ""))
                    return ActionResult(ok=False, status="denied",
                                        error=reason or "权限拒绝",
                                        meta={"tool": name, "args": args, "ctx": ctx})
                if decision == "ask":
                    approval_id = (str(_field(verdict, "approval_id") or "")
                                   or "ap_" + uuid.uuid4().hex[:12])
                    prompt = reason or f"工具 {name} 需要审批"
                    self._audit(ident, f"approval:{name}", name, "pending", prompt,
                                str(context.get("run") or ""))
                    return ActionResult(ok=False, status="needs_approval",
                                        error=prompt, approval_id=approval_id,
                                        meta={"tool": name, "args": args, "ctx": ctx})
        try:
            raw = self.registry.execute(name, dict(args or {}), context)
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as e:  # noqa: BLE001 —— 工具失败要回流给模型
            result = ActionResult(ok=False, status="error",
                                  error=f"{type(e).__name__}: {e}",
                                  duration_ms=int((time.time() - t0) * 1000),
                                  meta={"tool": name, "args": args, "ctx": ctx})
            self._audit(ident, f"tool:{name}", name, "error", result.error,
                        str(context.get("run") or ""))
            return result
        result = self._map(name, args, context, raw, t0)
        self._audit(ident, f"tool:{name}", name, result.status,
                    result.error, str(context.get("run") or ""))
        return result

    def request_approval(self, identity: dict, call: Any) -> ApprovalTicket:
        ident = identity or {}
        tool, args, ctx, approval_id, reason = self._call_parts(call)
        ticket = ApprovalTicket(
            id=approval_id or uuid.uuid4().hex[:16],
            tenant_id=str(ident.get("tenant_id") or ""),
            actor=str(ident.get("user_id") or ""),
            tool=tool,
            payload={"args": args, "ctx": ctx},
            reason=reason,
            status="pending",
        )
        self.queue.add(ticket)
        self._audit(ident, f"approval:{tool}", tool, "pending", reason,
                    str((ctx or {}).get("run") or ""))
        return ticket

    async def resume(self, ticket_id: str, decision: Any) -> ActionResult:
        ticket = self.queue.get(ticket_id)
        if ticket is None:
            return ActionResult(ok=False, status="denied",
                                error=f"审批单不存在：{ticket_id}")
        if ticket.status != "pending":
            # 幂等：同一审批单只执行一次，重复 resume 不重复调工具
            return ActionResult(ok=False, status="error",
                                error=f"审批单已处理：{ticket.status}",
                                meta={"ticket_id": ticket.id})
        approved, by, reason = self._decision(decision)
        ticket.status = "approved" if approved else "rejected"
        ticket.decided_at = time.time()
        ticket.decided_by = by
        if reason:
            ticket.reason = reason
        self.queue.update(ticket)
        identity = {"tenant_id": ticket.tenant_id, "user_id": ticket.actor}
        self._audit(identity, f"approval:{ticket.tool}", ticket.tool,
                    ticket.status, reason, "")
        if not approved:
            return ActionResult(ok=False, status="denied",
                                error=reason or f"审批被拒绝：{ticket.tool}",
                                meta={"ticket_id": ticket.id})
        ctx = dict(ticket.payload.get("ctx") or {})
        ctx["approved_override"] = True
        result = await self.call(identity, ticket.tool,
                                 dict(ticket.payload.get("args") or {}), ctx)
        result.meta.setdefault("ticket_id", ticket.id)
        return result

    # ------------------------------------------------------------ 内部
    def _map(self, name: str, args: dict, ctx: Any, raw: Any,
             t0: float) -> ActionResult:
        duration = int((time.time() - t0) * 1000)
        # 兼容 enter_harness.tooling.ToolRegistry / 执行器返回的 ToolResult
        if (not isinstance(raw, dict)
                and all(hasattr(raw, f) for f in ("ok", "content", "error"))):
            ok = bool(getattr(raw, "ok"))
            return ActionResult(
                ok=ok, status="ok" if ok else "error",
                result=getattr(raw, "content") if ok else None,
                error="" if ok else str(getattr(raw, "error") or "工具执行失败"),
                duration_ms=duration,
                meta={"tool": name, "args": args, "ctx": ctx})
        if not isinstance(raw, dict):
            return ActionResult(ok=True, result=raw, duration_ms=duration,
                                meta={"tool": name, "args": args, "ctx": ctx})
        if raw.get("needs_approval"):
            return ActionResult(
                ok=False, status="needs_approval",
                error=str(raw.get("error") or f"工具 {name} 需要审批"),
                approval_id=str(raw.get("approval_id") or uuid.uuid4().hex[:16]),
                duration_ms=int(raw.get("duration_ms") or duration),
                meta={"tool": name, "args": args, "ctx": ctx})
        if raw.get("denied"):
            return ActionResult(ok=False, status="denied",
                                error=str(raw.get("error") or "被策略拒绝"),
                                duration_ms=int(raw.get("duration_ms") or duration),
                                meta={"tool": name, "args": args, "ctx": ctx})
        if raw.get("ok"):
            return ActionResult(ok=True, status="ok", result=raw.get("result"),
                                duration_ms=int(raw.get("duration_ms") or duration),
                                meta={"tool": name, "args": args, "ctx": ctx})
        return ActionResult(ok=False, status="error",
                            error=str(raw.get("error") or "工具执行失败"),
                            duration_ms=int(raw.get("duration_ms") or duration),
                            meta={"tool": name, "args": args, "ctx": ctx})

    @staticmethod
    def _ctx(identity: dict, ctx: Any) -> dict:
        base = {
            "tenant": str(identity.get("tenant_id") or ""),
            "user": str(identity.get("user_id") or ""),
            "session": str(identity.get("session_id") or ""),
            "run": str(identity.get("run_id") or ""),
        }
        if isinstance(ctx, dict):
            base.update(ctx)
        elif hasattr(ctx, "as_dict"):
            try:
                base.update(ctx.as_dict())
            except Exception:  # noqa: BLE001
                pass
        return base

    @staticmethod
    def _call_parts(call: Any):
        if isinstance(call, ActionResult):
            meta = call.meta or {}
            return (str(meta.get("tool") or ""), dict(meta.get("args") or {}),
                    ToolActionPort._as_ctx(meta.get("ctx")), call.approval_id,
                    call.error)
        if isinstance(call, dict):
            return (str(call.get("name") or call.get("tool") or ""),
                    dict(call.get("args") or {}),
                    ToolActionPort._as_ctx(call.get("ctx")),
                    str(call.get("approval_id") or ""),
                    str(call.get("error") or call.get("reason") or ""))
        return "", {}, {}, "", ""

    @staticmethod
    def _as_ctx(value: Any) -> dict:
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "as_dict"):
            try:
                return dict(value.as_dict())
            except Exception:  # noqa: BLE001
                return {}
        return {}

    @staticmethod
    def _decision(decision: Any) -> tuple[bool, str, str]:
        by, reason = "", ""
        value = decision
        if isinstance(decision, dict):
            value = decision.get("decision", decision.get("approved", ""))
            by = str(decision.get("by") or decision.get("decided_by") or "")
            reason = str(decision.get("reason") or "")
        if isinstance(value, bool):
            return value, by, reason
        text = str(value or "").strip().lower()
        if text in APPROVED:
            return True, by, reason
        if text in REJECTED:
            return False, by, reason
        return False, by, reason or f"未知审批结论：{value}"

    def _audit(self, identity: dict, action: str, target: str, decision: str,
               reason: str, trace_id: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.emit(AuditEvent(
                tenant_id=str(identity.get("tenant_id") or ""),
                actor=str(identity.get("user_id") or ""),
                action=action, target=target, decision=decision,
                reason=reason, trace_id=trace_id))
        except Exception:  # noqa: BLE001 —— 审计失败不拖垮主链路
            pass
