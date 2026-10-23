"""上下文拼装指标（enter_harness.context.metrics）的用例。

只校验计数口径，不比对正文措辞；FakeContext 与
tests/test_unified_context_model.py 保持同一最小契约。
"""
import threading
import unittest

from enter_harness.context.metrics import (ContextMetrics, MetricsRegistry,
                                        summarize)
from enter_harness.context.model import Block, BlockMeta, content_hash
from enter_harness.context.pipeline import BlockCache, assemble


class FakeContext:
    """最小 ContextPort：元数据便宜、正文按需取。"""

    def __init__(self, blocks):
        self.blocks = {b.meta.id: b for b in blocks}
        self.fetches = []

    def list_blocks(self, scope="", scope_id=""):
        return [b.meta for b in self.blocks.values()]

    def get_block(self, block_id, version=0):
        self.fetches.append(block_id)
        b = self.blocks.get(block_id)
        if b is None:
            return None
        meta = BlockMeta(**dict(b.meta.__dict__))
        return Block(meta, b.text)


class DenyProfile:
    """示例策略：profile 类不注入，其余放行。"""

    def can(self, identity, action, target):
        return getattr(target, "kind", "") != "profile"


def blk(bid, kind, text, **kw):
    meta = BlockMeta(id=bid, kind=kind, **kw)
    b = Block(meta, text)
    meta.content_hash = content_hash(text)
    return b


class FromAssembleTests(unittest.TestCase):
    def test_01_full_injection_counts(self):
        """全量注入：块数、字符数、kind/authority 统计一致。"""
        ctx = FakeContext([
            blk("r", "procedural", "规则"),
            blk("s", "semantic", "事实"),
        ])
        out = assemble(context=ctx)
        m = ContextMetrics.from_assemble(out)

        self.assertEqual(m.blocks_total, 2)
        self.assertEqual(m.blocks_injected, 2)
        self.assertEqual(m.blocks_dropped, 0)
        self.assertEqual(m.blocks_truncated, 0)
        self.assertEqual(m.used_chars, len("规则") + len("事实"))
        self.assertEqual(m.budget_chars, 0)
        self.assertEqual(m.budget_utilization, 0.0)
        self.assertEqual(m.cache_hits, 0)
        self.assertEqual(m.cache_misses, 2)
        self.assertEqual(m.drop_reasons, {})
        self.assertEqual(m.injected_kinds, {"procedural": 1, "semantic": 1})
        self.assertEqual(m.authority_counts, {"MUST": 1, "IS": 1})

        d = m.to_dict()
        self.assertEqual(d["blocks_total"], 2)
        self.assertIsInstance(d["drop_reasons"], dict)
        self.assertEqual(d["injected_kinds"], {"procedural": 1, "semantic": 1})

    def test_02_budget_drop_reasons(self):
        """超出总预算的块整块丢弃，按 reason=budget 计数。"""
        ctx = FakeContext([
            blk("r", "procedural", "规" * 60),
            blk("s", "semantic", "事" * 60),
            blk("p", "profile", "偏" * 60),
        ])
        out = assemble(context=ctx, max_total_chars=120)
        m = ContextMetrics.from_assemble(out, budget_chars=120)

        self.assertEqual(m.blocks_total, 3)
        self.assertEqual(m.blocks_injected, 2)
        self.assertEqual(m.blocks_dropped, 1)
        self.assertEqual(m.drop_reasons, {"budget": 1})
        self.assertEqual(m.used_chars, 120)
        self.assertEqual(m.budget_chars, 120)
        self.assertAlmostEqual(m.budget_utilization, 1.0)
        self.assertEqual(m.injected_kinds, {"procedural": 1, "semantic": 1})

    def test_03_cache_hits_and_misses(self):
        """同一 BlockCache 两轮拼装：先 miss 后 hit。"""
        ctx = FakeContext([blk("r", "procedural", "规则")])
        cache = BlockCache()

        first = ContextMetrics.from_assemble(assemble(context=ctx, cache=cache))
        self.assertEqual((first.cache_hits, first.cache_misses), (0, 1))
        self.assertEqual(ctx.fetches, ["r"])

        second = ContextMetrics.from_assemble(assemble(context=ctx, cache=cache))
        self.assertEqual((second.cache_hits, second.cache_misses), (1, 0))
        self.assertEqual(ctx.fetches, ["r"])          # 第二轮没有重新抓正文

    def test_04_authority_and_kind_stats(self):
        """authority/kind 只统计注入块；被拒绝的块只进 drop_reasons。"""
        ctx = FakeContext([
            blk("s", "semantic", "事实"),
            blk("p", "profile", "偏好"),
        ])
        out = assemble(context=ctx, identity={"user_id": "u1"},
                       policy=DenyProfile())
        m = ContextMetrics.from_assemble(out)

        self.assertEqual(m.blocks_injected, 1)
        self.assertEqual(m.blocks_dropped, 1)
        self.assertEqual(m.blocks_total, 2)
        self.assertEqual(m.drop_reasons, {"denied": 1})
        self.assertEqual(m.injected_kinds, {"semantic": 1})
        self.assertEqual(m.authority_counts, {"IS": 1})

    def test_05_diagnostic_rows_ignored_and_duplicates_counted(self):
        """error/hash_mismatch 行不计块；同 id 的 duplicate_id 行照常计丢弃。"""
        result = {
            "used_chars": 5,
            "trace": [
                {"id": "context.list_blocks", "error": "RuntimeError"},
                {"id": "a", "hash_mismatch": True, "meta": "m", "body": "b"},
                {"id": "a", "kind": "semantic", "authority": "IS",
                 "cache": "hit", "chars": 5, "est_tokens": 3,
                 "truncated": False},
                {"id": "a", "dropped": True, "reason": "duplicate_id"},
                {"id": "b", "kind": "profile", "authority": "WANT",
                 "cache": "fetched", "chars": 4, "est_tokens": 2,
                 "truncated": True},
            ],
        }
        m = ContextMetrics.from_assemble(result, budget_chars=10)

        # 总数 = 2 个注入块（其中一个 truncated）+ 1 个 duplicate_id 丢弃
        self.assertEqual(m.blocks_total, 3)
        self.assertEqual(m.blocks_injected, 2)
        self.assertEqual(m.blocks_dropped, 1)
        self.assertEqual(m.blocks_truncated, 1)
        self.assertEqual(m.drop_reasons, {"duplicate_id": 1})
        self.assertEqual(m.cache_hits, 1)
        self.assertEqual(m.cache_misses, 1)
        self.assertEqual(m.used_chars, 5)
        self.assertAlmostEqual(m.budget_utilization, 0.5)


class MergeAndSummarizeTests(unittest.TestCase):
    def setUp(self):
        self.m1 = ContextMetrics.from_assemble(
            assemble(context=FakeContext([blk("a", "semantic", "abcd")])),
            budget_chars=100)
        self.m2 = ContextMetrics.from_assemble(
            assemble(context=FakeContext([
                blk("b", "procedural", "xx"),
                blk("c", "profile", "yy"),
            ])),
            budget_chars=20)

    def test_06_merge_sums_counts_and_recomputes_utilization(self):
        merged = self.m1.merge(self.m2)

        self.assertEqual(merged.blocks_total, 3)
        self.assertEqual(merged.blocks_injected, 3)
        self.assertEqual(merged.used_chars, 8)
        self.assertEqual(merged.budget_chars, 100)     # 取第一个非零预算
        self.assertAlmostEqual(merged.budget_utilization, 0.08)
        self.assertEqual(merged.cache_misses, 3)
        self.assertEqual(merged.injected_kinds,
                         {"semantic": 1, "procedural": 1, "profile": 1})
        self.assertEqual(merged.authority_counts,
                         {"IS": 1, "MUST": 1, "WANT": 1})

        # 原对象不被修改
        self.assertEqual(self.m1.used_chars, 4)
        self.assertEqual(self.m2.budget_chars, 20)

    def test_07_merge_empty_uses_other_budget(self):
        merged = ContextMetrics().merge(self.m2)
        self.assertEqual(merged.blocks_total, 2)
        self.assertEqual(merged.budget_chars, 20)
        self.assertAlmostEqual(merged.budget_utilization, 0.2)

    def test_08_summarize_totals_and_average(self):
        s = summarize([self.m1, self.m2])

        self.assertEqual(s["count"], 2)
        self.assertEqual(s["blocks_total"], 3)
        self.assertEqual(s["used_chars"], 8)
        self.assertEqual(s["budget_chars"], 100)
        self.assertAlmostEqual(s["budget_utilization"], 0.08)
        self.assertAlmostEqual(s["avg_utilization"], (0.04 + 0.2) / 2)

        empty = summarize([])
        self.assertEqual(empty["count"], 0)
        self.assertEqual(empty["blocks_total"], 0)
        self.assertEqual(empty["avg_utilization"], 0.0)


class EmptyResultTests(unittest.TestCase):
    def test_09_empty_assemble_is_all_zero(self):
        """空集正常返回，指标全 0，不报错。"""
        m = ContextMetrics.from_assemble(assemble(context=FakeContext([])),
                                         budget_chars=100)
        self.assertEqual(m.blocks_total, 0)
        self.assertEqual(m.blocks_injected, 0)
        self.assertEqual(m.blocks_dropped, 0)
        self.assertEqual(m.used_chars, 0)
        self.assertEqual(m.budget_chars, 100)
        self.assertEqual(m.budget_utilization, 0.0)
        self.assertEqual(m.drop_reasons, {})
        self.assertEqual(m.authority_counts, {})
        self.assertEqual(m.injected_kinds, {})

    def test_10_none_result_tolerated(self):
        m = ContextMetrics.from_assemble(None)
        self.assertEqual(m, ContextMetrics())


class MetricsRegistryTests(unittest.TestCase):
    def test_empty_snapshot(self):
        snap = MetricsRegistry().snapshot()
        self.assertEqual(snap["count"], 0)
        self.assertEqual(snap["window"], 0)
        self.assertEqual(snap["max_utilization"], 0.0)

    def test_record_aggregates(self):
        reg = MetricsRegistry()
        reg.record(ContextMetrics(blocks_injected=1, used_chars=40,
                                  budget_chars=100, budget_utilization=0.4))
        snap = reg.record(ContextMetrics(blocks_injected=2, used_chars=60,
                                         budget_chars=100, budget_utilization=0.6))
        self.assertEqual(snap["count"], 2)
        self.assertEqual(snap["blocks_injected"], 3)
        self.assertEqual(snap["used_chars"], 100)
        self.assertAlmostEqual(snap["budget_utilization"], 1.0)
        self.assertAlmostEqual(snap["avg_utilization"], 0.5)
        self.assertAlmostEqual(snap["max_utilization"], 0.6)
        self.assertEqual(snap["window"], 2)
        self.assertEqual(len(reg), 2)

    def test_record_assemble_and_none(self):
        reg = MetricsRegistry()
        reg.record(None)
        snap = reg.record_assemble({"trace": [], "used_chars": 0},
                                   budget_chars=50)
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["budget_chars"], 50)

    def test_window_keeps_latest(self):
        reg = MetricsRegistry(max_items=2)
        for used in (10, 20, 30):
            reg.record(ContextMetrics(used_chars=used, budget_chars=100))
        snap = reg.snapshot()
        self.assertEqual(snap["count"], 2)
        self.assertEqual(snap["used_chars"], 50)
        self.assertEqual(len(reg), 2)

    def test_reset(self):
        reg = MetricsRegistry()
        reg.record(ContextMetrics(used_chars=1))
        reg.reset()
        self.assertEqual(len(reg), 0)
        self.assertEqual(reg.snapshot()["count"], 0)

    def test_thread_safe_recording(self):
        reg = MetricsRegistry()

        def work():
            for _ in range(100):
                reg.record(ContextMetrics(blocks_injected=1))

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = reg.snapshot()
        self.assertEqual(snap["count"], 400)
        self.assertEqual(snap["blocks_injected"], 400)


if __name__ == "__main__":
    unittest.main()
