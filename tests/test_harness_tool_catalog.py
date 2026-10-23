"""工具目录：可见性过滤 + 装配期校验。"""
import unittest

from enter_harness.contracts import ToolSpec
from enter_harness.harness import PortSet, ToolActionPort
from enter_harness.tooling import ToolRegistry as FunctionToolRegistry
from enter_harness.tools import ToolRegistry


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register("echo", lambda text="": text, "回显", parallel=True)
    reg.register("tenant_tool", lambda: "x", tenants=("t1",))
    reg.register("role_tool", lambda: "x", roles=("admin",))
    reg.register("scoped", lambda: "x", scope="project")
    reg.register("tagged", lambda: "x", tags=("project",))
    return reg


def _names(tools) -> set[str]:
    return {str(t.get("name")) for t in tools}


class ToolVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.port = ToolActionPort(_registry())

    def test_unrestricted_tools_are_visible(self):
        names = _names(self.port.list_tools({"tenant_id": "t2", "roles": ["user"]}))
        self.assertIn("echo", names)
        self.assertIn("scoped", names)

    def test_tenant_restricted_hidden_for_other_tenant(self):
        names = _names(self.port.list_tools({"tenant_id": "t2"}))
        self.assertNotIn("tenant_tool", names)

    def test_tenant_restricted_visible_for_matching_tenant(self):
        names = _names(self.port.list_tools({"tenant_id": "t1"}))
        self.assertIn("tenant_tool", names)

    def test_tenant_restricted_fails_closed_without_identity(self):
        names = _names(self.port.list_tools({}))
        self.assertNotIn("tenant_tool", names)
        self.assertNotIn("role_tool", names)

    def test_role_restricted_requires_intersection(self):
        self.assertNotIn("role_tool", _names(self.port.list_tools({"roles": ["user"]})))
        self.assertIn("role_tool", _names(self.port.list_tools({"roles": ["admin"]})))

    def test_role_ids_alias(self):
        names = _names(self.port.list_tools({"role_ids": ["admin"]}))
        self.assertIn("role_tool", names)

    def test_metadata_fallback(self):
        reg = ToolRegistry()
        reg.register("m", lambda: "x", metadata={"tenants": ["t9"]})
        port = ToolActionPort(reg)
        self.assertNotIn("m", _names(port.list_tools({"tenant_id": "t1"})))
        self.assertIn("m", _names(port.list_tools({"tenant_id": "t9"})))

    def test_scope_filter_preserved(self):
        names = _names(self.port.list_tools({}, scope="project"))
        self.assertEqual(names, {"scoped", "tagged"})


class FunctionToolCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def _port(self):
        reg = FunctionToolRegistry()
        reg.register("echo", lambda text="": f"echo:{text}", "回显",
                     parallel_safe=True)
        reg.register("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        return ToolActionPort(reg)

    def test_list_tools_returns_dicts(self):
        tools = self._port().list_tools({"tenant_id": "t1"})
        self.assertTrue(all(isinstance(t, dict) for t in tools))
        by_name = {t["name"]: t for t in tools}
        self.assertTrue(by_name["echo"]["parallel"])
        self.assertTrue(by_name["echo"]["enabled"])

    def test_validate_accepts_function_tools(self):
        self.assertEqual(self._port().validate(), [])

    async def test_call_maps_tool_result_success(self):
        result = await self._port().call({"tenant_id": "t1"}, "echo",
                                         {"text": "hi"})
        self.assertTrue(result.ok)
        self.assertEqual(result.result, "echo:hi")

    async def test_call_maps_tool_result_failure(self):
        result = await self._port().call({"tenant_id": "t1"}, "boom", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertIn("RuntimeError", result.error)


class ToolCallVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_denies_hidden_tool(self):
        called = []
        reg = ToolRegistry()
        reg.register("secret", lambda: called.append(1) or "x", tenants=("t1",))
        result = await ToolActionPort(reg).call({"tenant_id": "t2"}, "secret", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "denied")
        self.assertEqual(called, [])

    async def test_call_allows_matching_identity(self):
        reg = ToolRegistry()
        reg.register("secret", lambda: "ok", tenants=("t1",))
        result = await ToolActionPort(reg).call({"tenant_id": "t1"}, "secret", {})
        self.assertTrue(result.ok)
        self.assertEqual(result.result, "ok")

    async def test_approved_override_skips_visibility(self):
        reg = ToolRegistry()
        reg.register("secret", lambda: "ok", tenants=("t1",))
        result = await ToolActionPort(reg).call(
            {"tenant_id": "t2"}, "secret", {}, {"approved_override": True})
        self.assertTrue(result.ok)


class ToolSpecValidationTests(unittest.TestCase):
    def test_spec_reports_schema_errors(self):
        spec = ToolSpec(name="x", parameters={
            "type": "object", "properties": {"n": {"type": "integer"}},
            "required": ["missing"]})
        self.assertIn("parameters_required_unknown:missing",
                      spec.validation_errors())

    def test_register_rejects_bad_name(self):
        with self.assertRaises(ValueError):
            ToolRegistry().register("", lambda: 1)

    def test_register_rejects_non_callable(self):
        with self.assertRaises(ValueError):
            ToolRegistry().register("x", "not-callable")

    def test_register_rejects_bad_schema(self):
        with self.assertRaises(ValueError):
            ToolRegistry().register("x", lambda: 1, parameters={"type": "array"})

    def test_register_rejects_string_tenants(self):
        with self.assertRaises(ValueError):
            ToolRegistry().register("x", lambda: 1, tenants="t1")

    def test_duplicate_replace_false(self):
        reg = ToolRegistry()
        reg.register("x", lambda: 1)
        with self.assertRaises(ValueError):
            reg.register("x", lambda: 2, replace=False)

    def test_duplicate_replace_default_keeps_latest(self):
        reg = ToolRegistry()
        reg.register("x", lambda: 1)
        reg.register("x", lambda: 2)
        self.assertTrue(reg.has("x"))

    def test_effective_risk_derives_from_danger(self):
        self.assertEqual(ToolSpec(name="x", danger=True).effective_risk, "high")
        self.assertEqual(ToolSpec(name="x").effective_risk, "normal")
        self.assertEqual(
            ToolSpec(name="x", danger=True, risk="low").effective_risk, "low")

    def test_register_accepts_governance_enums(self):
        reg = ToolRegistry()
        reg.register("x", lambda: 1, risk="high", side_effect="write",
                     approval="always")
        item = reg.list_tools()[0]
        self.assertEqual(item["risk"], "high")
        self.assertEqual(item["side_effect"], "write")
        self.assertEqual(item["approval"], "always")

    def test_register_rejects_unknown_enums(self):
        for kwargs in ({"risk": "critical"}, {"side_effect": "unknown"},
                       {"approval": "sometimes"}):
            with self.assertRaises(ValueError):
                ToolRegistry().register("x", lambda: 1, **kwargs)

    def test_parallel_conflicts_with_write_side_effect(self):
        with self.assertRaises(ValueError):
            ToolRegistry().register("x", lambda: 1, parallel=True,
                                    side_effect="write")

    def test_to_dict_exposes_visibility_fields(self):
        reg = ToolRegistry()
        reg.register("x", lambda: 1, scope="project",
                     tenants=("t1",), roles=("admin",))
        item = reg.list_tools()[0]
        self.assertEqual(item["scope"], "project")
        self.assertEqual(item["tenants"], ["t1"])
        self.assertEqual(item["roles"], ["admin"])

    def test_validate_reports_registry_problems(self):
        class BadRegistry:
            def list_tools(self):
                return [{"name": "x", "parameters": "bad"}, {"name": "x"}]

            async def execute(self, name, args, ctx):
                return {"ok": True}

        errs = ToolActionPort(BadRegistry()).validate()
        self.assertIn("x:parameters_not_object", errs)
        self.assertIn("duplicate_tool:x", errs)


class _Id:
    def resolve(self, req=None):
        return {"tenant_id": "t1", "user_id": "u1"}


class _Ctx:
    def list_blocks(self, scope="", scope_id=""):
        return []

    def get_block(self, block_id, version=0):
        return None


class _Mem:
    def query(self, req=None):
        return []

    def upsert(self, item):
        return None

    def archive(self, ids):
        return 0


class _Know:
    def search(self, query, top_k=4, scope=""):
        return []

    def get_chunk(self, chunk_id):
        return None


class _Policy:
    def can(self, identity, action, target):
        return True


class _Ingest:
    def collect(self, req=None):
        return []


class _State:
    def load(self, thread_id):
        return None

    def save(self, thread_id, state):
        return None

    def checkpoint(self, thread_id, node, state=None):
        return "ck"

    def resume(self, thread_id):
        return None

    def list_checkpoints(self, thread_id):
        return []


class _Verify:
    def check(self, result, criteria=None, ctx=None):
        return None


class _Audit:
    def emit(self, event):
        return event

    def query(self, filter=None):
        return []


class PortSetToolValidationTests(unittest.TestCase):
    def test_check_rejects_invalid_tool_catalog(self):
        class BadRegistry:
            def list_tools(self):
                return [{"name": "x", "parameters": "bad"}]

            async def execute(self, name, args, ctx):
                return {"ok": True}

        ports = PortSet(
            identity=_Id(), context=_Ctx(), memory=_Mem(), knowledge=_Know(),
            policy=_Policy(), ingest=_Ingest(),
            action=ToolActionPort(BadRegistry()), state=_State(),
            verify=_Verify(), audit=_Audit())
        self.assertEqual(ports.invalid(), [])
        with self.assertRaises(TypeError) as ctx:
            ports.check()
        self.assertIn("action:x:parameters_not_object", str(ctx.exception))

    def test_check_passes_with_valid_tool_catalog(self):
        ports = PortSet(
            identity=_Id(), context=_Ctx(), memory=_Mem(), knowledge=_Know(),
            policy=_Policy(), ingest=_Ingest(),
            action=ToolActionPort(_registry()), state=_State(),
            verify=_Verify(), audit=_Audit())
        self.assertTrue(ports.check())


if __name__ == "__main__":
    unittest.main()
