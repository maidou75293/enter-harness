"""agent_core 错误体系。

原则：core 只抛“语义错误”，业务层（app）决定如何展示/降级。
"""


class AgentError(Exception):
    """所有 agent_core 错误的基类。

    code/retryable/details 供宿主统一映射；子类可覆盖默认值。
    """

    code = "agent_error"
    retryable = False

    def __init__(self, message: str = "", *, code: str | None = None,
                 retryable: bool | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = str(code)
        if retryable is not None:
            self.retryable = bool(retryable)
        self.details = dict(details or {})


class ConfigError(AgentError):
    """缺少必要配置（模型名、API key 等）。"""


class ProviderError(AgentError):
    """模型 provider 调用失败。"""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class ToolError(AgentError):
    """工具执行失败（可恢复：调用方通常把消息留给模型）。"""


class ToolNotFound(ToolError):
    def __init__(self, name: str):
        super().__init__(f"未知工具：{name}")
        self.name = name


class ToolNotAllowed(ToolError):
    """工具不在白名单 / 被策略拒绝。"""


class ToolApprovalRequired(ToolError):
    def __init__(self, name: str, approval_id: str = "", reason: str = ""):
        super().__init__(reason or f"工具 {name} 需要审批")
        self.name = name
        self.approval_id = approval_id


class ToolTimeout(ToolError):
    pass


class ContextOverflow(AgentError):
    """上下文超出预算且无法裁剪。"""


class MaxStepsExceeded(AgentError):
    """主循环达到 max_steps 仍未结束。"""


class RunTimeout(AgentError):
    """主循环超时。"""


class Interrupted(AgentError):
    """运行被外部中断。"""


class SubagentDepthExceeded(AgentError):
    """子代理嵌套超过 max_depth。"""


class SandboxUnavailable(AgentError):
    """请求的沙箱后端不可用且不允许回退。"""


class ScopeError(AgentError):
    """缺租户 / 跨租户 / 角色不符。"""

    code = "scope_error"


class BudgetExceeded(AgentError):
    """预算耗尽。"""

    code = "budget_exceeded"


class PermissionDenied(AgentError):
    """策略拒绝。"""

    code = "permission_denied"


class BlockedDependency(AgentError):
    """上游阻塞。"""

    code = "blocked_dependency"


class ModelError(AgentError):
    """模型调用失败（可重试）。"""

    code = "model_error"
    retryable = True


class StoreError(AgentError):
    """存储失败（可重试）。"""

    code = "store_error"
    retryable = True


class NotFoundError(StoreError):
    """要读取/更新的对象不存在。"""

    code = "not_found"
    retryable = False


class ConflictError(AgentError):
    """版本/并发冲突。"""

    code = "conflict"

    def __init__(self, message: str = "", *, current_version: int = 0,
                 expected_version: int = 0, **kw):
        super().__init__(message, **kw)
        self.current_version = int(current_version)
        self.expected_version = int(expected_version)


class CancelledError(AgentError):
    """调用方取消。"""

    code = "cancelled"


class SchemaError(AgentError):
    """返回格式不合格（不重试）。"""

    code = "schema_error"
