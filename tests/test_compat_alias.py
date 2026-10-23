"""旧包名兼容：`agent_core` → `enter_harness`。"""
import unittest


class CompatAliasTests(unittest.TestCase):
    def test_top_level_import(self):
        import agent_core
        import enter_harness

        self.assertIs(agent_core.Agent, enter_harness.Agent)
        self.assertEqual(agent_core.__version__, enter_harness.__version__)
        self.assertIn("Agent", agent_core.__all__)

    def test_submodule_is_same_module(self):
        import agent_core.harness as old
        import enter_harness.harness as new

        self.assertIs(old, new)
        from agent_core.harness import build_local_ports

        self.assertIs(build_local_ports, new.build_local_ports)

    def test_nested_submodule(self):
        import agent_core.context.model as old
        import enter_harness.context.model as new

        self.assertIs(old, new)
        self.assertIs(old.BlockMeta, new.BlockMeta)

    def test_lazy_exports_still_work(self):
        import agent_core

        self.assertTrue(callable(agent_core.build_react_subgraph))

    def test_unknown_attribute_raises(self):
        import agent_core

        with self.assertRaises(AttributeError):
            agent_core.no_such_symbol  # noqa: B018


if __name__ == "__main__":
    unittest.main()
