"""agent_core 契约（Protocol）。

core 只认这些协议，不认 app 的具体实现：
- Settings   配置读取（app.Store 天然满足）
- Store      会话/消息/记忆等持久化（只需要实际用到的方法）
- Provider   模型调用
- Policy     工具权限判定（allow / ask / deny）
- MemoryProvider  上下文记忆注入
- EventSink  事件出口（UI 实时流 / 数据库落盘）
- Sandbox    命令沙箱
- SkillSource 技能清单与按需加载

全部按“结构类型”定义，业务类无需继承。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------- 设置 / 存储


@runtime_checkable
class Settings(Protocol):
    """配置读取。app.Store 已实现同名方法，可直接注入。"""

    def get_config(self, key: str, default: Any = None) -> Any: ...

    def get_bool(self, key: str, default: bool = False) -> bool: ...

    def get_int(self, key: str, default: int = 0) -> int: ...

    def get_float(self, key: str, default: float = 0.0) -> float: ...


@runtime_checkable
class Store(Protocol):
    """core 用到的会话/消息/快照持久化能力子集。"""

    def create_session(self, sid: str, title: str = "", tenant_id: str = "default") -> dict: ...

    def list_sessions(self, tenant_id: str | None = None) -> list: ...

    def get_session(self, sid: str) -> dict | None: ...

    def delete_session(self, sid: str) -> None: ...

    def add_message(self, sid: str, role: str, content: str,
                    meta: dict | None = None) -> int: ...

    def list_messages(self, sid: str, limit: int | None = None) -> list: ...

    def list_active_messages(self, sid: str, limit: int | None = None) -> list: ...

    def mark_compacted(self, ids: list) -> None: ...

    def save_snapshot(self, thread_id: str, session_id: str,
                      plan: dict, state: dict) -> None: ...


@runtime_checkable
class SessionStore(Store, Protocol):
    pass


# ---------------------------------------------------------------- 模型


@runtime_checkable
class Provider(Protocol):
    """OpenAI 兼容 provider：chat 返回 assistant 消息 dict。"""

    async def chat(self, messages: list, tools: list | None = None, tool_choice: Any = None,
                   response_format: Any = None, temperature: float | None = None) -> dict: ...

    async def complete(self, messages: list, kind: str = "chat") -> str: ...


# ---------------------------------------------------------------- 工具 / 权限


@dataclass
class ToolSpec:
    """工具定义。parameters 使用 JSON Schema。"""

    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    danger: bool = False
    source: str = "builtin"
    tags: tuple = ()
    metadata: dict = field(default_factory=dict)
    # 默认白名单：None = 不限制；list = 仅这些 name 可被某个 subagent 使用
    enabled: bool = True
    # 只读、无副作用、彼此独立 → 同批工具调用可以并发执行
    parallel: bool = False
    # 可见性契约：空 scope = 不限制；非空时按请求的 scope / tags 过滤
    scope: str = ""
    # 空 = 所有租户可见；非空 = 仅这些 tenant_id 可见（默认拒绝）
    tenants: tuple = ()
    # 空 = 所有角色可见；非空 = 与 identity.roles 有交集才可见（默认拒绝）
    roles: tuple = ()
    # 治理声明：空 = 未声明；risk 空时由 danger 推导，approval 空时由策略决定
    risk: str = ""            # low / normal / high
    side_effect: str = ""     # none / read / write / external
    approval: str = ""        # policy / always / never（声明，最终由策略判定）

    @property
    def effective_risk(self) -> str:
        if self.risk:
            return self.risk
        return "high" if self.danger else "normal"

    def validation_errors(self) -> list[str]:
        """装配期校验：返回错误列表，空列表表示合法。"""
        errs: list[str] = []
        name = str(self.name or "")
        if not name:
            errs.append("name_required")
        elif len(name) > 128:
            errs.append("name_too_long")
        elif any(ch.isspace() for ch in name):
            errs.append("name_has_whitespace")
        if not isinstance(self.parameters, dict):
            errs.append("parameters_not_object")
        else:
            typ = self.parameters.get("type")
            if typ is not None and typ != "object":
                errs.append("parameters_type_not_object")
            props = self.parameters.get("properties")
            if props is not None and not isinstance(props, dict):
                errs.append("parameters_properties_not_object")
            required = self.parameters.get("required")
            if required is not None:
                if not isinstance(required, (list, tuple)):
                    errs.append("parameters_required_not_list")
                elif not all(isinstance(x, str) for x in required):
                    errs.append("parameters_required_not_strings")
                elif isinstance(props, dict):
                    missing = [x for x in required if x not in props]
                    if missing:
                        errs.append(
                            "parameters_required_unknown:" + ",".join(missing))
        if not isinstance(self.scope, str):
            errs.append("scope_not_string")
        for field in ("tenants", "roles"):
            value = getattr(self, field, ())
            if isinstance(value, str) or not isinstance(value, (list, tuple)):
                errs.append(f"{field}_not_list")
            elif not all(isinstance(x, str) and x for x in value):
                errs.append(f"{field}_has_empty")
        for field in ("danger", "enabled", "parallel"):
            if not isinstance(getattr(self, field), bool):
                errs.append(f"{field}_not_bool")
        for field, allowed in (("risk", ("", "low", "normal", "high")),
                               ("side_effect", ("", "none", "read", "write",
                                                "external")),
                               ("approval", ("", "policy", "always", "never"))):
            value = getattr(self, field, "")
            if not isinstance(value, str):
                errs.append(f"{field}_not_string")
            elif value not in allowed:
                errs.append(f"{field}_unknown:{value}")
        if self.parallel and self.side_effect in ("write", "external"):
            errs.append("parallel_with_side_effect")
        return errs

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "danger": self.danger,
            "source": self.source,
            "tags": list(self.tags),
            "enabled": self.enabled,
            "parallel": self.parallel,
            "scope": self.scope,
            "tenants": list(self.tenants),
            "roles": list(self.roles),
            "risk": self.effective_risk,
            "side_effect": self.side_effect,
            "approval": self.approval,
        }
        if self.metadata:
            d.update(self.metadata)
        return d

    def to_openai(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters or {"type": "object", "properties": {}},
        }}


@dataclass
class ToolContext:
    """一次工具调用的运行期上下文（供策略/审计/沙箱使用）。"""

    tenant: str = "default"
    session: str = ""
    run: str = ""
    user: str = ""
    args: dict = field(default_factory=dict)
    step: int = 0
    subagent_depth: int = 0

    def as_dict(self) -> dict:
        return {"tenant": self.tenant, "session": self.session, "run": self.run,
                "user": self.user, "args": self.args, "step": self.step,
                "subagent_depth": self.subagent_depth}


@dataclass
class PolicyDecision:
    """权限判定结果。decision: allow | ask | deny。"""

    decision: str = "allow"
    reason: str = ""
    approval_id: str = ""
    scope: str = ""

    @classmethod
    def allow(cls, reason: str = "") -> "PolicyDecision":
        return cls("allow", reason)

    @classmethod
    def deny(cls, reason: str = "") -> "PolicyDecision":
        return cls("deny", reason)

    @classmethod
    def ask(cls, reason: str = "", approval_id: str = "", scope: str = "user") -> "PolicyDecision":
        return cls("ask", reason, approval_id, scope)

    def to_dict(self) -> dict:
        d = {"decision": self.decision, "reason": self.reason}
        if self.approval_id:
            d["approval_id"] = self.approval_id
        if self.scope:
            d["scope"] = self.scope
        # 兼容旧决策字典（needs_approval）
        if self.decision == "ask":
            d["needs_approval"] = True
        return d


@runtime_checkable
class Policy(Protocol):
    """权限策略。core 只调用 check()；具体规则由外部注入。"""

    def check(self, tool_meta: dict, ctx: dict | None = None) -> Any: ...


def normalize_decision(raw: Any) -> PolicyDecision:
    """把 PolicyDecision / 旧式 dict / None 统一成 PolicyDecision。"""
    if raw is None:
        return PolicyDecision.allow()
    if isinstance(raw, PolicyDecision):
        return raw
    if isinstance(raw, str):
        return PolicyDecision(raw)
    if isinstance(raw, dict):
        d = str(raw.get("decision") or ("ask" if raw.get("needs_approval") else "allow"))
        if d == "needs_approval":
            d = "ask"
        return PolicyDecision(d, str(raw.get("reason") or ""),
                              str(raw.get("approval_id") or ""), str(raw.get("scope") or ""))
    return PolicyDecision.allow()


@runtime_checkable
class Approver(Protocol):
    """交互式审批（可选）。execute() 返回 True 表示放行。"""

    async def approve(self, tool_meta: dict, ctx: dict, decision: PolicyDecision) -> bool: ...


# ---------------------------------------------------------------- 记忆 / 事件


@runtime_checkable
class MemoryProvider(Protocol):
    def inject(self, query: str, token_budget: int = 600) -> dict: ...


@runtime_checkable
class EventSink(Protocol):
    async def emit(self, event: dict) -> None: ...


# ---------------------------------------------------------------- 技能 / 沙箱


@runtime_checkable
class SkillSource(Protocol):
    def list(self) -> list: ...

    def manifest(self) -> str: ...

    def load(self, name: str) -> str: ...


@runtime_checkable
class Sandbox(Protocol):
    """把命令包进隔离环境；返回 (可执行命令, 实际后端)。"""

    def wrap_shell(self, command: str, **kwargs) -> tuple[str, str]: ...
