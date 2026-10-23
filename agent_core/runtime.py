"""运行期上下文（contextvars）。

core 不依赖业务层的 tenants 模块；业务层可以把这几个 contextvar 作为
「当前租户/会话/运行/用户」的唯一事实来源（app.tenants 已做别名）。
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass

DEFAULT_TENANT = "default"

current_tenant: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_core_tenant", default=DEFAULT_TENANT)
current_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_core_session", default="")
current_run: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_core_run", default="")
current_user: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_core_user", default="")
# 子代理嵌套深度（SubagentRunner 校验）
current_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "agent_core_depth", default=0)


def normalize_tenant(tid) -> str:
    """租户 id 只允许 [A-Za-z0-9_-]，避免路径穿越。"""
    t = str(tid or "").strip()
    if not t:
        return DEFAULT_TENANT
    if not all(c.isalnum() or c in "_-" for c in t):
        return DEFAULT_TENANT
    return t[:64]


@dataclass
class Snapshot:
    tenant: str
    session: str
    run: str
    user: str
    depth: int

    def restore(self):
        current_tenant.set(self.tenant)
        current_session.set(self.session)
        current_run.set(self.run)
        current_user.set(self.user)
        current_depth.set(self.depth)


def snapshot() -> Snapshot:
    return Snapshot(current_tenant.get(), current_session.get(), current_run.get(),
                    current_user.get(), current_depth.get())


@contextlib.contextmanager
def bind(*, tenant=None, session=None, run=None, user=None, depth=None):
    """在 with 块内绑定运行上下文，退出时按 token 精确恢复（嵌套安全）。"""
    tokens = []
    try:
        if tenant is not None:
            tokens.append((current_tenant, current_tenant.set(normalize_tenant(tenant))))
        if session is not None:
            tokens.append((current_session, current_session.set(str(session))))
        if run is not None:
            tokens.append((current_run, current_run.set(str(run))))
        if user is not None:
            tokens.append((current_user, current_user.set(str(user))))
        if depth is not None:
            tokens.append((current_depth, current_depth.set(int(depth))))
        yield
    finally:
        for var, tok in reversed(tokens):
            try:
                var.reset(tok)
            except (ValueError, RuntimeError):
                pass
