"""Agent / Session 管理层 API（模块级，不依赖 HTTP）。

一个租户下可有多个智能体；一个智能体下可有多个会话。``Agent`` 仍然是
运行入口（按 ``Scope`` 一实例），本模块只负责“智能体/会话”的增删改查与
租户、智能体之间的隔离。

默认智能体（``agent_id="default"``）用于兼容单智能体场景：旧调用方不指定
``agent_id`` 时行为不变。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .domain import AgentRecord, AgentSettings, Scope, SessionRecord
from .errors import ConfigError, NotFoundError
from .ports import AgentStorePort, Clock, IdGen

DEFAULT_AGENT_ID = "default"


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class _UuidIds:
    def new_id(self, prefix: str = "") -> str:
        import uuid
        return f"{prefix}{uuid.uuid4().hex[:12]}" if prefix else uuid.uuid4().hex[:12]


def _tenant(tenant_id: str) -> str:
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ConfigError("tenant_id 不能为空")
    return tid


def _settings(config: AgentSettings | dict | None) -> AgentSettings:
    if config is None:
        return AgentSettings()
    if isinstance(config, AgentSettings):
        return config
    if isinstance(config, dict):
        known = set(AgentSettings.__dataclass_fields__)
        unknown = set(config) - known
        if unknown:
            raise ConfigError(f"未知的 agent 配置项：{sorted(unknown)}")
        return AgentSettings(**config)
    raise ConfigError("config 必须是 AgentSettings 或 dict")


class AgentService:
    """租户内的智能体/会话管理。

    存储由宿主注入（``AgentStorePort``）；``InMemoryStore`` 已实现该端口。
    """

    def __init__(self, store: AgentStorePort, *,
                 ids: IdGen | None = None, clock: Clock | None = None):
        if store is None:
            raise ConfigError("缺少 store")
        self.store = store
        self.ids = ids or _UuidIds()
        self.clock = clock or _SystemClock()

    # ---------------- Agent ----------------
    def create_agent(self, tenant_id: str, name: str, *, role: str = "",
                     config: AgentSettings | dict | None = None,
                     agent_id: str = "", status: str = "active") -> AgentRecord:
        tid = _tenant(tenant_id)
        nm = str(name or "").strip()
        if not nm:
            raise ConfigError("智能体名称不能为空")
        aid = str(agent_id or "").strip() or self.ids.new_id("ag_")
        rec = AgentRecord(
            agent_id=aid, tenant_id=tid, name=nm, role=str(role or ""),
            config=_settings(config), status=str(status or "active"),
            created_at=self.clock.now().timestamp())
        return self.store.create_agent(rec)

    def get_agent(self, tenant_id: str, agent_id: str) -> AgentRecord | None:
        return self.store.get_agent(_tenant(tenant_id),
                                    str(agent_id or "").strip())

    def require_agent(self, tenant_id: str, agent_id: str) -> AgentRecord:
        rec = self.get_agent(tenant_id, agent_id)
        if rec is None:
            raise NotFoundError(f"智能体不存在：{agent_id}")
        return rec

    def list_agents(self, tenant_id: str) -> list[AgentRecord]:
        return self.store.list_agents(_tenant(tenant_id))

    def update_agent(self, tenant_id: str, agent_id: str, *,
                     name: str | None = None, role: str | None = None,
                     config: AgentSettings | dict | None = None,
                     status: str | None = None) -> AgentRecord:
        old = self.require_agent(tenant_id, agent_id)
        rec = AgentRecord(
            agent_id=old.agent_id, tenant_id=old.tenant_id,
            name=old.name if name is None else str(name).strip(),
            role=old.role if role is None else str(role or ""),
            config=old.config if config is None else _settings(config),
            status=old.status if status is None else str(status or "active"),
            created_at=old.created_at)
        if not rec.name:
            raise ConfigError("智能体名称不能为空")
        return self.store.update_agent(rec)

    def delete_agent(self, tenant_id: str, agent_id: str) -> bool:
        return self.store.delete_agent(_tenant(tenant_id),
                                       str(agent_id or "").strip())

    def ensure_default_agent(self, tenant_id: str) -> AgentRecord:
        """单智能体兼容：没有指定智能体时使用/创建默认智能体。"""
        tid = _tenant(tenant_id)
        store = self.store
        ensure = getattr(store, "ensure_default_agent", None)
        if ensure is not None:
            return ensure(tid)
        rec = store.get_agent(tid, DEFAULT_AGENT_ID)
        if rec is not None:
            return rec
        return store.create_agent(AgentRecord(
            agent_id=DEFAULT_AGENT_ID, tenant_id=tid, name="默认智能体",
            created_at=self.clock.now().timestamp()))

    def scope(self, tenant_id: str, agent_id: str = "", *, user_id: str = "",
              role_ids: tuple[str, ...] | list[str] = ()) -> Scope:
        """构造运行作用域；不传 agent_id 时用默认智能体。"""
        aid = str(agent_id or "").strip() or DEFAULT_AGENT_ID
        if str(agent_id or "").strip():
            self.require_agent(tenant_id, aid)
        else:
            self.ensure_default_agent(tenant_id)
        return Scope(tenant_id=_tenant(tenant_id), agent_id=aid,
                     user_id=user_id, role_ids=role_ids)

    # ---------------- Session ----------------
    def create_session(self, tenant_id: str, agent_id: str = "", *,
                       user_id: str = "", title: str = "",
                       session_id: str = "") -> SessionRecord:
        tid = _tenant(tenant_id)
        aid = str(agent_id or "").strip()
        if not aid:
            self.ensure_default_agent(tid)
            aid = DEFAULT_AGENT_ID
        else:
            self.require_agent(tid, aid)
        now = self.clock.now().timestamp()
        rec = SessionRecord(
            session_id=str(session_id or "").strip() or self.ids.new_id("s_"),
            tenant_id=tid, agent_id=aid, user_id=str(user_id or "").strip(),
            title=str(title or ""), status="active",
            created_at=now, updated_at=now)
        return self.store.create_session(rec)

    def get_session(self, tenant_id: str, session_id: str) -> SessionRecord | None:
        return self.store.get_session(_tenant(tenant_id),
                                      str(session_id or "").strip())

    def list_sessions(self, tenant_id: str, agent_id: str) -> list[SessionRecord]:
        return self.store.list_sessions(_tenant(tenant_id),
                                        str(agent_id or "").strip())

    def touch_session(self, tenant_id: str, session_id: str) -> SessionRecord:
        """会话活跃时间更新（宿主在每轮结束后调用）。"""
        rec = self.get_session(tenant_id, session_id)
        if rec is None:
            raise NotFoundError(f"会话不存在：{session_id}")
        from dataclasses import replace
        return self.store.update_session(replace(
            rec, updated_at=self.clock.now().timestamp()))


__all__ = ["AgentService", "DEFAULT_AGENT_ID"]
