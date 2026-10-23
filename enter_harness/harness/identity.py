"""IdentityPort 的默认实现：静态身份目录 + 最小权限合并。

定位：
- 生产身份仍应来自账号系统；这个实现用于本地、测试、单租户部署；
- 人身份（user）和智能体身份（agent）分开登记；
- 合并时取能力交集，智能体不能放大用户的权限；
- `expires_at` 过期即拒绝，短时凭证由宿主刷新。
"""
from __future__ import annotations

import time
from typing import Any, Callable


class StaticIdentityPort:
    """按 tenant/user 查表；可选叠加 agent 身份。"""

    def __init__(self, users: dict | None = None, agents: dict | None = None,
                 *, allow_anonymous: bool = False,
                 clock: Callable[[], float] | None = None):
        self.users = dict(users or {})
        self.agents = dict(agents or {})
        self.allow_anonymous = bool(allow_anonymous)
        self._clock = clock or time.time

    def resolve(self, req: Any = None) -> dict:
        req = dict(req or {})
        tenant_id = str(req.get("tenant_id") or "")
        user_id = str(req.get("user_id") or "")
        agent_id = str(req.get("agent_id") or "")

        user = self._find(self.users, tenant_id, user_id)
        if user is None:
            if not self.allow_anonymous:
                raise PermissionError(f"身份不存在：tenant={tenant_id} user={user_id}")
            user = {"tenant_id": tenant_id, "user_id": user_id,
                    "roles": [], "capabilities": [], "writable_scopes": []}

        ident = dict(user)
        ident["tenant_id"] = str(ident.get("tenant_id") or tenant_id)
        ident["user_id"] = str(ident.get("user_id") or user_id)
        if tenant_id and ident["tenant_id"] and ident["tenant_id"] != tenant_id:
            raise PermissionError("身份租户与请求租户不一致")

        agent = self._find(self.agents, tenant_id, agent_id) if agent_id else None
        if agent_id and agent is None:
            raise PermissionError(f"智能体身份不存在：tenant={tenant_id} agent={agent_id}")
        if agent is not None:
            ident["agent_id"] = str(agent.get("agent_id") or agent_id)
            ident["roles"] = sorted(set(ident.get("roles") or ())
                                    | set(agent.get("roles") or ()))
            # 能力与可写范围取交集：智能体只收紧，不放大
            ident["capabilities"] = self._intersect(
                ident.get("capabilities"), agent.get("capabilities"))
            ident["writable_scopes"] = self._intersect(
                ident.get("writable_scopes"), agent.get("writable_scopes"))

        expires_at = float(ident.get("expires_at") or 0)
        if expires_at and expires_at <= self._clock():
            raise PermissionError("身份已过期")
        return ident

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _find(table: dict, tenant_id: str, key: str) -> dict | None:
        if not key:
            return None
        candidates = [(tenant_id, key), key]
        for k in candidates:
            value = table.get(k)
            if value is not None:
                return dict(value)
        return None

    @staticmethod
    def _intersect(left, right) -> list:
        if left is None:
            return list(right or ())
        if right is None:
            return list(left or ())
        return sorted(set(left) & set(right))
