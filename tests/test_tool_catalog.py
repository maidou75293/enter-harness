"""工具目录分层（ToolCatalog）：来源标记、每来源配额、统计与批量装配。"""
import unittest

from enter_harness.contracts import ToolSpec
from enter_harness.tools import ToolRegistry
from enter_harness.tools.catalog import SOURCE_KINDS, ToolCatalog


def _echo(**kwargs):
    """测试用简单 handler。"""
    return kwargs


class ToolCatalogBasicsTests(unittest.TestCase):
    """来源标记与查询。"""

    def test_default_source_is_builtin(self):
        """不传 source 时默认归为 builtin，并能按来源查回。"""
        cat = ToolCatalog()
        cat.register("calc", _echo)
        self.assertEqual(cat.sources_of("calc"), "builtin")
        self.assertEqual([d["name"] for d in cat.by_source("builtin")], ["calc"])
        self.assertEqual(cat.by_source("mcp"), [])

    def test_register_passes_options_through(self):
        """tags / tenants / roles / risk 等参数应透传给底层注册表。"""
        cat = ToolCatalog()
        cat.register("search", _echo, source="skill", tags=("web",),
                     tenants=("t1",), roles=("admin",), risk="low",
                     parallel=True)
        item = cat.by_source("skill")[0]
        self.assertEqual(item["tags"], ["web"])
        self.assertEqual(item["tenants"], ["t1"])
        self.assertEqual(item["roles"], ["admin"])
        self.assertEqual(item["risk"], "low")
        self.assertTrue(item["parallel"])

    def test_unknown_source_rejected(self):
        """未知来源必须抛 ValueError，且不会注册到注册表。"""
        cat = ToolCatalog()
        with self.assertRaises(ValueError) as ctx:
            cat.register("bad", _echo, source="plugin")
        self.assertIn("plugin", str(ctx.exception))
        self.assertEqual(cat.list_tools(), [])

    def test_unknown_source_in_quotas_rejected(self):
        """配额表里出现未知来源也应尽早报错。"""
        with self.assertRaises(ValueError):
            ToolCatalog(quotas={"plugin": 1})

    def test_sources_of_unknown_tool(self):
        """查询不存在的工具来源时抛 KeyError。"""
        with self.assertRaises(KeyError):
            ToolCatalog().sources_of("nope")


class ToolCatalogQuotaTests(unittest.TestCase):
    """注册期配额与跨来源独立性。"""

    def test_mcp_quota(self):
        """mcp 达到配额后再注册报错，消息含来源 / 配额 / 当前数量。"""
        cat = ToolCatalog(quotas={"mcp": 2})
        cat.register("m1", _echo, source="mcp")
        cat.register("m2", _echo, source="mcp")
        with self.assertRaises(ValueError) as ctx:
            cat.register("m3", _echo, source="mcp")
        msg = str(ctx.exception)
        self.assertIn("mcp", msg)
        self.assertIn("2", msg)
        self.assertEqual(len(cat.by_source("mcp")), 2)

    def test_quotas_are_per_source(self):
        """一个来源满额不影响其它来源。"""
        cat = ToolCatalog(quotas={"mcp": 1, "skill": 1})
        cat.register("m1", _echo, source="mcp")
        with self.assertRaises(ValueError):
            cat.register("m2", _echo, source="mcp")
        cat.register("s1", _echo, source="skill")
        cat.register("b1", _echo)  # builtin 未设配额，不限制
        cat.register("b2", _echo)
        self.assertEqual(cat.counts(), {"builtin": 2, "mcp": 1, "skill": 1,
                                        "subagent": 0, "host": 0})

    def test_unregister_releases_quota(self):
        """unregister 成功后释放名额；注销不存在的工具返回 False。"""
        cat = ToolCatalog(quotas={"mcp": 1})
        cat.register("m1", _echo, source="mcp")
        with self.assertRaises(ValueError):
            cat.register("m2", _echo, source="mcp")
        self.assertTrue(cat.unregister("m1"))
        self.assertFalse(cat.unregister("m1"))
        cat.register("m2", _echo, source="mcp")
        self.assertEqual([d["name"] for d in cat.by_source("mcp")], ["m2"])

    def test_hot_update_requires_unregister_first(self):
        """配额是注册期限制：满额时替换同名工具也会被挡住。"""
        cat = ToolCatalog(quotas={"mcp": 1})
        cat.register("m1", _echo, source="mcp")
        with self.assertRaises(ValueError):
            cat.register("m1", _echo, source="mcp")
        cat.unregister("m1")
        cat.register("m1", _echo, source="mcp", description="updated")
        self.assertEqual(cat.by_source("mcp")[0]["description"], "updated")


class ToolCatalogStatsTests(unittest.TestCase):
    """统计、概览与批量装配。"""

    def test_counts_zero_filled(self):
        """counts 必须包含全部来源类别，没有工具的补 0。"""
        cat = ToolCatalog()
        counts = cat.counts()
        self.assertEqual(set(counts), set(SOURCE_KINDS))
        self.assertEqual(counts, {kind: 0 for kind in SOURCE_KINDS})
        cat.register("b1", _echo)
        cat.register("h1", _echo, source="host")
        self.assertEqual(cat.counts()["builtin"], 1)
        self.assertEqual(cat.counts()["host"], 1)
        self.assertEqual(cat.counts()["subagent"], 0)

    def test_summary(self):
        """summary 汇总总数、分类计数与配额。"""
        cat = ToolCatalog(quotas={"mcp": 3})
        cat.register("b1", _echo)
        cat.register("m1", _echo, source="mcp")
        summary = cat.summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["counts"]["builtin"], 1)
        self.assertEqual(summary["counts"]["mcp"], 1)
        self.assertEqual(summary["quotas"], {"mcp": 3})

    def test_passthrough_and_validate(self):
        """specs / list_tools / validate 走底层注册表。"""
        registry = ToolRegistry()
        cat = ToolCatalog(registry=registry)
        cat.register("b1", _echo)
        self.assertIs(cat.registry, registry)
        self.assertEqual([s.name for s in cat.specs()], ["b1"])
        self.assertEqual([d["name"] for d in cat.list_tools()], ["b1"])
        self.assertEqual(cat.validate(), [])

    def test_from_specs(self):
        """from_specs 按 spec 自带 source 批量装配，并同样受配额约束。"""
        specs = [
            (ToolSpec(name="b1"), _echo),
            (ToolSpec(name="m1", source="mcp"), _echo),
        ]
        cat = ToolCatalog.from_specs(specs, quotas={"mcp": 1})
        self.assertEqual(cat.sources_of("b1"), "builtin")
        self.assertEqual(cat.sources_of("m1"), "mcp")
        with self.assertRaises(ValueError):
            ToolCatalog.from_specs(
                [(ToolSpec(name="m2", source="mcp"), _echo),
                 (ToolSpec(name="m3", source="mcp"), _echo)],
                quotas={"mcp": 1})


if __name__ == "__main__":
    unittest.main()


class DisabledQuotaTests(unittest.TestCase):
    def test_disabled_tools_still_count_against_quota(self):
        from enter_harness.tools import ToolCatalog

        catalog = ToolCatalog(quotas={"mcp": 1})
        catalog.register("a", lambda: "a", source="mcp", enabled=False)
        with self.assertRaises(ValueError):
            catalog.register("b", lambda: "b", source="mcp")
        self.assertEqual(catalog.counts()["mcp"], 1)
        self.assertEqual(catalog.summary()["total"], 1)
        self.assertEqual(catalog.by_source("mcp"), [])
