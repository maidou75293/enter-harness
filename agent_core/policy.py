"""权限模型：allow / ask / deny。

core 只定义契约和一个**通用默认实现**；业务规则（租户信任、审批时效、白名单存储）
由 app 注入自己的 Policy，core 不硬编码任何业务名单。

RulePolicy 匹配顺序（与 Claude Code 一致：deny > ask > allow）：
    1. deny 规则（fnmatch）
    2. ask 规则
    3. allow 规则
    4. danger 工具的默认决策（默认 ask）
    5. default
"""
from __future__ import annotations

from fnmatch import fnmatch

from .contracts import PolicyDecision


class RulePolicy:
    def __init__(self, *, allow=(), deny=(), ask=(), default: str = "allow",
                 danger_decision: str = "ask", enabled: bool = True):
        self.allow = list(allow)
        self.deny = list(deny)
        self.ask = list(ask)
        self.default = default if default in ("allow", "ask", "deny") else "allow"
        self.danger_decision = (danger_decision if danger_decision in ("allow", "ask", "deny")
                                else "ask")
        self.enabled = enabled

    @staticmethod
    def _match(name: str, patterns) -> bool:
        return any(name == p or fnmatch(name, p) for p in patterns)

    def check(self, tool_meta: dict, ctx: dict | None = None) -> PolicyDecision:
        ctx = ctx or {}
        if not self.enabled:
            return PolicyDecision.deny("工具总开关已关闭")
        # 运行期上下文可以额外限制（例如子代理白名单）
        allowed = ctx.get("allowed_tools")
        name = str(tool_meta.get("name") or "")
        if allowed is not None and name not in set(allowed):
            return PolicyDecision.deny(f"工具不在当前允许列表：{name}")
        if self._match(name, self.deny):
            return PolicyDecision.deny(f"工具 {name} 被策略禁止")
        if self._match(name, self.ask):
            return PolicyDecision.ask(f"工具 {name} 需要审批", scope="user")
        if self._match(name, self.allow):
            return PolicyDecision.allow("命中允许规则")
        if tool_meta.get("danger"):
            if self.danger_decision == "deny":
                return PolicyDecision.deny(f"危险工具 {name} 被禁止")
            if self.danger_decision == "ask":
                return PolicyDecision.ask(f"危险工具 {name} 需要审批", scope="user")
        if self.default == "deny":
            return PolicyDecision.deny("默认拒绝")
        if self.default == "ask":
            return PolicyDecision.ask(f"工具 {name} 需要审批", scope="user")
        return PolicyDecision.allow("默认允许")
