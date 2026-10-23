"""工具目录分层：按来源（builtin / mcp / skill / subagent / host）管理工具。

在治理版 :class:`~enter_harness.tools.registry.ToolRegistry` 之上加一层
“来源分组 + 注册配额”，方便宿主在装配期看清每个工具来自哪里，并限制
外部来源（如 MCP）能注册多少工具。

设计要点：
- 来源只能是 :data:`SOURCE_KINDS` 之一，非法来源直接抛 ``ValueError``。
- 配额是**注册期**限制：每个来源最多注册多少个工具。热更新（替换同名工具）
  请先 ``unregister(name)`` 再 ``register(...)``，否则会被配额挡住。
- 其余注册参数（tags / tenants / roles / scope / risk 等）原样透传给
  ``ToolRegistry.register``。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .registry import ToolRegistry

#: 允许的工具来源分类：内置、MCP、技能、子代理、宿主动态注入。
SOURCE_KINDS = ("builtin", "mcp", "skill", "subagent", "host")


class ToolCatalog:
    """按来源分层的工具目录，支持每来源注册配额。

    参数：
        registry: 底层治理版 ``ToolRegistry``；不传则自动新建一个。
        quotas: 形如 ``{"mcp": 3}`` 的来源配额；未列出的来源不限制。

    注意：配额只在**注册期**生效。热更新时请先 ``unregister`` 再 ``register``。
    """

    def __init__(self, registry: ToolRegistry | None = None,
                 quotas: dict[str, int] | None = None):
        self.registry = registry if registry is not None else ToolRegistry()
        self.quotas: dict[str, int] = {}
        for source, quota in dict(quotas or {}).items():
            self._check_source(source)
            if not isinstance(quota, int) or isinstance(quota, bool) or quota < 0:
                raise ValueError(
                    f"来源 {source!r} 的配额必须是非负整数，收到 {quota!r}")
            self.quotas[source] = quota

    # ------------------------------------------------------------ 内部校验
    @staticmethod
    def _check_source(source: str) -> str:
        """校验来源合法，返回来源字符串。"""
        if source not in SOURCE_KINDS:
            raise ValueError(
                f"未知工具来源：{source!r}；合法取值：{', '.join(SOURCE_KINDS)}")
        return source

    def _check_quota(self, source: str) -> None:
        """检查某来源是否还有注册名额，超出则抛 ``ValueError``。"""
        quota = self.quotas.get(source)
        if quota is None:
            return
        current = self.counts().get(source, 0)
        if current >= quota:
            raise ValueError(
                f"来源 {source!r} 已达注册配额：配额 {quota}，"
                f"当前数量 {current}；如需热更新请先 unregister 再 register")

    # ------------------------------------------------------------ 注册
    def register(self, name: str, handler: Callable, *,
                 source: str = "builtin", **options: Any) -> None:
        """注册工具并标记来源。

        - ``source`` 不在 :data:`SOURCE_KINDS` 中 → ``ValueError``；
        - 该来源已满配额 → ``ValueError``（消息含来源、配额、当前数量）；
        - 其余 ``options`` 原样透传给 ``ToolRegistry.register``。

        配额是注册期限制；热更新同来源工具时请先 ``unregister``。
        """
        source = self._check_source(source)
        self._check_quota(source)
        self.registry.register(name, handler, source=source, **options)

    def unregister(self, name: str) -> bool:
        """注销工具；成功返回 ``True``，工具不存在返回 ``False``。

        注销后该工具占用的来源配额会被释放。
        """
        return self.registry.unregister(name)

    # ------------------------------------------------------------ 查询
    def by_source(self, source: str) -> list[dict]:
        """返回某来源下所有（已启用）工具的定义字典，保持注册顺序。"""
        source = self._check_source(source)
        return [d for d in self.list_tools() if d.get("source") == source]

    def counts(self) -> dict:
        """返回各来源的**已注册**工具数量（含 ``enabled=False``）。

        配额按同一口径计数，避免用停用工具绕过上限；
        所有 :data:`SOURCE_KINDS` 都会出现（含 0）。
        """
        counts = {kind: 0 for kind in SOURCE_KINDS}
        for item in self.registry.all_specs():
            source = item.get("source", "builtin")
            counts[source] = counts.get(source, 0) + 1
        return counts

    def sources_of(self, name: str) -> str:
        """返回指定工具的来源标记；工具不存在抛 ``KeyError``。"""
        try:
            return str(self.registry.meta(name).get("source", ""))
        except KeyError:
            raise KeyError(f"未知工具：{name}") from None

    def summary(self) -> dict:
        """返回目录概览：``{"total": n, "counts": {...}, "quotas": {...}}``。"""
        counts = self.counts()
        return {"total": sum(counts.values()),
                "counts": counts,
                "quotas": dict(self.quotas)}

    # ------------------------------------------------------------ 透传
    def specs(self) -> list:
        """透传底层注册表的 ``specs()``（已启用的 ToolSpec 列表）。"""
        return self.registry.specs()

    def list_tools(self) -> list[dict]:
        """透传底层注册表的 ``list_tools()``（已启用工具的定义字典）。"""
        return self.registry.list_tools()

    def validate(self) -> list[str]:
        """透传底层注册表的装配期自检结果。"""
        return self.registry.validate()

    # ------------------------------------------------------------ 批量装配
    @classmethod
    def from_specs(cls, specs_with_handlers: Iterable[tuple],
                   registry: ToolRegistry | None = None,
                   quotas: dict[str, int] | None = None) -> "ToolCatalog":
        """从 ``(ToolSpec, handler)`` 序列批量装配目录，方便宿主启动时使用。

        每个 spec 自带 ``source``，同样受来源校验和配额约束。
        """
        catalog = cls(registry=registry, quotas=quotas)
        for spec, handler in specs_with_handlers:
            source = catalog._check_source(getattr(spec, "source", "builtin"))
            catalog._check_quota(source)
            catalog.registry.register_spec(spec, handler)
        return catalog
