"""D1 次序：assemble 的 sort_key 必须与 select_records 的 scope_rank 一致。"""
import unittest

from agent_core.context.model import (Block, BlockMeta, SCOPE_ORDER_BY_KIND,
                                      scope_rank)
from agent_core.context.pipeline import assemble


class FakeContext:
    def __init__(self, blocks):
        self.blocks = {b.meta.id: b for b in blocks}

    def list_blocks(self, scope="", scope_id=""):
        return [b.meta for b in self.blocks.values()]

    def get_block(self, block_id, version=0):
        return self.blocks.get(block_id)


class ContextOrderTests(unittest.TestCase):
    def test_sort_key_uses_kind_aware_scope_rank(self):
        for kind, order in SCOPE_ORDER_BY_KIND.items():
            for left in order:
                for right in order:
                    a = BlockMeta(id="a", kind=kind, scope=left, scope_id="x")
                    b = BlockMeta(id="b", kind=kind, scope=right, scope_id="x")
                    self.assertEqual(
                        a.sort_key[1] < b.sort_key[1],
                        scope_rank(kind, left) < scope_rank(kind, right),
                        f"{kind}: {left} vs {right}",
                    )

    def test_semantic_prefers_project_over_platform(self):
        project = BlockMeta(id="p", kind="semantic", scope="project",
                            scope_id="p1")
        platform = BlockMeta(id="g", kind="semantic", scope="platform")
        self.assertLess(project.sort_key, platform.sort_key)

    def test_procedural_prefers_platform_over_project(self):
        platform = BlockMeta(id="g", kind="procedural", scope="platform")
        project = BlockMeta(id="p", kind="procedural", scope="project",
                            scope_id="p1")
        self.assertLess(platform.sort_key, project.sort_key)

    def test_profile_prefers_user_over_project(self):
        user = BlockMeta(id="u", kind="profile", scope="user", scope_id="u1")
        project = BlockMeta(id="p", kind="profile", scope="project",
                            scope_id="p1")
        self.assertLess(user.sort_key, project.sort_key)

    def test_assemble_keeps_kind_aware_order(self):
        blocks = [
            Block(BlockMeta(id="g", kind="semantic", scope="platform"), "平台事实"),
            Block(BlockMeta(id="p", kind="semantic", scope="project",
                            scope_id="p1"), "项目事实"),
            Block(BlockMeta(id="u", kind="semantic", scope="user",
                            scope_id="u1"), "用户事实"),
        ]
        result = assemble(context=FakeContext(blocks),
                          identity={"tenant_id": "t1", "user_id": "u1"},
                          max_total_chars=1000)
        self.assertEqual([b["id"] for b in result["blocks"]], ["p", "g", "u"])


if __name__ == "__main__":
    unittest.main()
