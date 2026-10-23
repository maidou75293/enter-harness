"""工具注册表与统一执行。

执行链：
    白名单 → before_tool hooks → Policy.check
        ├─ deny  → {"ok": False, "denied": True}
        ├─ ask   → approver 放行 / 返回 needs_approval（上层挂起）
        └─ allow → handler（同步丢线程，支持 async）
                   → after_tool hooks → 大输出治理 → {"ok": True, "result": ...}

设计要点：
- 权限策略**外部注入**（Policy 协议），core 不写死危险工具清单。
- handler 异常转成错误结果，不打断主循环（模型能看到错误并重试）。
- 工具参数用 JSON Schema 描述；可选严格校验。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..contracts import (Approver, PolicyDecision, ToolContext, ToolSpec,
                         normalize_decision)
from ..errors import ToolNotFound
from ..hooks.manager import HookDenied, HookManager
from ..runtime import (current_run, current_session, current_tenant, current_user,
                       current_depth, normalize_tenant)
from ..settings import as_settings
from .output import OutputLimiter


@dataclass
class _Registered:
    spec: ToolSpec
    handler: Callable = field(repr=False)


class ToolRegistry:
    """线程内共享的工具总线。业务层可继承并覆写 `_audit` / `_check_extra` 等钩子。"""

    def __init__(self, policy=None, *, hooks: HookManager | None = None, tracer=None,
                 settings=None, output_limiter: OutputLimiter | None = None,
                 approver: Approver | None = None, strict_schema: bool = False,
                 output_root: Callable[[], Path | None] | None = None):
        self.policy = policy
        self.hooks = hooks or HookManager()
        self.tracer = tracer
        self.settings = as_settings(settings)
        self.limiter = output_limiter or OutputLimiter()
        self.approver = approver
        self.strict_schema = strict_schema
        self._output_root = output_root
        self.whitelist: set[str] | None = None
        self._tools: dict[str, _Registered] = {}
        self._pending_approval = None
        self._last_backend = ""

    # ------------------------------------------------------------ 注册
    def register(self, name: str, handler: Callable, description: str = "",
                 parameters: dict | None = None, danger: bool = False,
                 source: str = "builtin", tags: tuple = (),
                 metadata: dict | None = None, enabled: bool = True,
                 parallel: bool = False, scope: str = "",
                 tenants: tuple = (), roles: tuple = (),
                 risk: str = "", side_effect: str = "", approval: str = "",
                 replace: bool = True) -> None:
        """注册工具。装配期校验失败直接抛 ValueError。

        replace=False 时重名也抛错；默认保留原来的“后注册覆盖”行为。
        scope / tenants / roles 是可见性契约，由 ActionPort 在 list_tools 时过滤。
        """
        if not callable(handler):
            raise ValueError(f"工具 {name!r} 的 handler 不可调用")
        if isinstance(tenants, str) or isinstance(roles, str):
            raise ValueError(f"工具 {name!r} 的 tenants/roles 必须是列表或元组")
        if not replace and name in self._tools:
            raise ValueError(f"工具重名：{name}")
        spec = ToolSpec(name=name, description=description,
                        parameters=parameters or {"type": "object", "properties": {}},
                        danger=danger, source=source, tags=tuple(tags),
                        metadata=dict(metadata or {}), enabled=enabled,
                        parallel=parallel, scope=scope,
                        tenants=tuple(tenants), roles=tuple(roles),
                        risk=risk, side_effect=side_effect, approval=approval)
        errs = spec.validation_errors()
        if errs:
            raise ValueError(f"工具 {name!r} 定义不合法：" + ", ".join(errs))
        self._tools[name] = _Registered(spec, handler)

    def register_spec(self, spec: ToolSpec, handler: Callable,
                      replace: bool = True) -> None:
        if not callable(handler):
            raise ValueError(f"工具 {spec.name!r} 的 handler 不可调用")
        if not replace and spec.name in self._tools:
            raise ValueError(f"工具重名：{spec.name}")
        errs = spec.validation_errors()
        if errs:
            raise ValueError(f"工具 {spec.name!r} 定义不合法：" + ", ".join(errs))
        self._tools[spec.name] = _Registered(spec, handler)

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def validate(self) -> list[str]:
        """装配期自检：返回所有工具的问题，空列表表示通过。"""
        errs: list[str] = []
        for name, reg in self._tools.items():
            prefix = f"{name}: "
            if not callable(reg.handler):
                errs.append(prefix + "handler_not_callable")
            errs.extend(prefix + e for e in reg.spec.validation_errors())
        return errs

    # ------------------------------------------------------------ 查询
    def list_tools(self) -> list:
        return [r.spec.to_dict() for r in self._tools.values() if r.spec.enabled]

    def specs(self) -> list:
        return [r.spec for r in self._tools.values() if r.spec.enabled]

    def has(self, name: str) -> bool:
        return name in self._tools and self._tools[name].spec.enabled

    def is_parallel_safe(self, name: str) -> bool:
        """该工具能否与同批其它调用并发（未知/停用工具按不安全处理）。"""
        reg = self._tools.get(name)
        return bool(reg and reg.spec.enabled and reg.spec.parallel)

    def meta(self, name: str) -> dict:
        return self._tools[name].spec.to_dict()

    def handler(self, name: str) -> Callable:
        return self._tools[name].handler

    def openai_tools(self, names: list | None = None) -> list:
        allow = set(names) if names is not None else None
        if allow is None and self.whitelist is not None:
            allow = self.whitelist
        out = []
        for r in self._tools.values():
            if not r.spec.enabled:
                continue
            if allow is not None and r.spec.name not in allow:
                continue
            out.append(r.spec.to_openai())
        return out

    # ------------------------------------------------------------ 白名单 / 子集
    def set_whitelist(self, names) -> None:
        self.whitelist = None if names is None else {str(x) for x in names}

    def subset(self, names, *, clone_hooks: bool = False) -> "ToolRegistry":
        """返回只含指定工具、且白名单固定的视图（子代理用）。"""
        reg = ToolRegistry(self.policy, hooks=self.hooks if clone_hooks else HookManager(),
                           tracer=self.tracer, settings=self.settings,
                           output_limiter=self.limiter, approver=self.approver,
                           strict_schema=self.strict_schema, output_root=self._output_root)
        for n in names:
            if n in self._tools:
                reg._tools[n] = self._tools[n]
        reg.set_whitelist(names)
        return reg

    def all_parallel_safe(self, names) -> bool:
        return all(self.is_parallel_safe(n) for n in names)

    def pop_pending_approval(self):
        v = self._pending_approval
        self._pending_approval = None
        return v

    # ------------------------------------------------------------ 执行
    def _ctx(self, ctx: ToolContext | dict | None, args: dict, step: int = 0) -> ToolContext:
        if isinstance(ctx, ToolContext):
            c = ctx
            c.args = args
            return c
        base = dict(ctx or {})
        return ToolContext(
            tenant=normalize_tenant(base.get("tenant") or current_tenant.get()),
            session=str(base.get("session") or current_session.get()),
            run=str(base.get("run") or current_run.get()),
            user=str(base.get("user") or current_user.get()),
            args=args, step=int(base.get("step", step)),
            subagent_depth=int(base.get("subagent_depth", current_depth.get())),
        )

    def _output_root_path(self) -> Path | None:
        if self._output_root is None:
            return None
        try:
            return self._output_root()
        except Exception:  # noqa: BLE001
            return None

    def _check_extra(self, spec: ToolSpec, ctx: ToolContext) -> PolicyDecision | None:
        """业务层扩展点（如按租户限制 MCP）。返回 None 表示不拦截。"""
        return None

    def _audit(self, spec: ToolSpec, ctx: ToolContext, decision: str,
               detail: str = "", dur_ms: int = 0) -> None:
        """业务层扩展点：调用审计。"""

    def _validate(self, spec: ToolSpec, args: dict) -> str | None:
        if not self.strict_schema:
            return None
        params = spec.parameters or {}
        required = params.get("required") or []
        missing = [k for k in required if k not in args]
        if missing:
            return f"缺少必填参数：{', '.join(missing)}"
        props = params.get("properties") or {}
        for k, v in args.items():
            typ = (props.get(k) or {}).get("type")
            if typ == "string" and not isinstance(v, str):
                return f"参数 {k} 应为 string"
            if typ == "integer" and not isinstance(v, int):
                return f"参数 {k} 应为 integer"
            if typ == "number" and not isinstance(v, (int, float)):
                return f"参数 {k} 应为 number"
            if typ == "array" and not isinstance(v, list):
                return f"参数 {k} 应为 array"
            if typ == "object" and not isinstance(v, dict):
                return f"参数 {k} 应为 object"
        return None

    async def execute(self, name: str, args: dict | None = None,
                      ctx: ToolContext | dict | None = None, step: int = 0) -> dict:
        args = dict(args or {})
        t0 = time.time()
        # 审批恢复：调用方携带 approved_override 时不再重复走 ask
        approved_override = bool(isinstance(ctx, dict)
                                 and ctx.get("approved_override"))
        if name not in self._tools:
            return {"ok": False, "denied": True, "error": f"未知工具：{name}"}
        reg = self._tools[name]
        spec = reg.spec
        c = self._ctx(ctx, args, step)

        if not spec.enabled:
            return {"ok": False, "denied": True, "error": f"工具已停用：{name}"}
        if self.whitelist is not None and name not in self.whitelist:
            self._audit(spec, c, "deny", "不在工具白名单", _ms(t0))
            return {"ok": False, "denied": True, "error": f"工具不在白名单：{name}"}

        # ---- before_tool hooks（可改参数 / 拒绝） ----
        try:
            args = await self.hooks.before_tool(name, args, c)
        except HookDenied as e:
            self._audit(spec, c, "deny", f"hook: {e.reason}", _ms(t0))
            return {"ok": False, "denied": True, "error": e.reason}
        c.args = args

        # ---- 参数校验 ----
        verr = self._validate(spec, args)
        if verr:
            self._audit(spec, c, "deny", verr, _ms(t0))
            return {"ok": False, "error": verr}

        # ---- 权限策略（外部注入） ----
        if (self.policy is not None and not approved_override
                and not spec.metadata.get("approved_override")):
            raw = self.policy.check(spec.to_dict(), c.as_dict())
            decision = normalize_decision(raw)
            if decision.decision == "deny":
                self._audit(spec, c, "deny", decision.reason, _ms(t0))
                return {"ok": False, "denied": True, "error": decision.reason}
            if decision.decision == "ask":
                if self.approver is not None:
                    try:
                        ok = await self.approver.approve(spec.to_dict(), c.as_dict(), decision)
                    except Exception:  # noqa: BLE001
                        ok = False
                    if not ok:
                        return self._ask_result(spec, c, decision, t0)
                else:
                    return self._ask_result(spec, c, decision, t0)

        # ---- 业务扩展检查（租户 / MCP 等） ----
        extra = self._check_extra(spec, c)
        if extra is not None:
            d = normalize_decision(extra)
            if d.decision == "deny":
                self._audit(spec, c, "deny", d.reason, _ms(t0))
                return {"ok": False, "denied": True, "error": d.reason}

        # ---- 调用 ----
        await self._trace("tool_start", tool=name, **c.as_dict())
        try:
            if inspect.iscoroutinefunction(reg.handler):
                result = await reg.handler(**args)
            else:
                result = await asyncio.to_thread(reg.handler, **args)
                if inspect.isawaitable(result):
                    result = await result
        except ToolNotFound:
            raise
        except asyncio.TimeoutError:
            self._audit(spec, c, "error", "timeout", _ms(t0))
            r = {"ok": False, "error": f"工具超时：{name}"}
            await self.hooks.emit("tool_error", tool=name, error=r["error"], ctx=c)
            await self._trace("tool_end", tool=name, ok=False, error=r["error"])
            return r
        except Exception as e:  # noqa: BLE001 —— 失败要留在上下文里，不吞
            err = f"{type(e).__name__}: {e}"
            self._audit(spec, c, "error", err, _ms(t0))
            r = {"ok": False, "error": err}
            await self.hooks.emit("tool_error", tool=name, error=err, ctx=c)
            await self.hooks.after_tool(name, args, r, c)
            await self._trace("tool_end", tool=name, ok=False, error=err)
            return r

        out = self.limiter(name, result, self._output_root_path())
        r = {"ok": True, "result": out, "duration_ms": _ms(t0)}
        r = await self.hooks.after_tool(name, args, r, c)
        self._audit(spec, c, "allow", "ok", _ms(t0))
        await self._trace("tool_end", tool=name, ok=True, result=str(out)[:2000])
        return r

    def _ask_result(self, spec: ToolSpec, ctx: ToolContext, decision: PolicyDecision,
                    t0: float) -> dict:
        self._pending_approval = {"approval_id": decision.approval_id,
                                  "tool": spec.name, "scope": decision.scope}
        self._audit(spec, ctx, "needs_approval",
                    f"{decision.reason} / 单号 {decision.approval_id}", _ms(t0))
        return {"ok": False, "needs_approval": True,
                "approval_id": decision.approval_id,
                "scope": decision.scope,
                "error": decision.reason or f"工具 {spec.name} 需要审批"}

    async def _trace(self, event: str, **payload) -> None:
        if self.tracer is not None:
            try:
                await self.tracer.emit(event, **payload)
            except Exception:  # noqa: BLE001
                pass


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)
