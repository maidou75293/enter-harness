"""KnowledgePort 默认 SQLite 实现的契约用例。"""
import hashlib
import time
import unittest

from enter_harness.harness.knowledge import SQLiteKnowledgePort


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SQLiteKnowledgePortTests(unittest.TestCase):
    def setUp(self):
        self.port = SQLiteKnowledgePort(":memory:")

    def tearDown(self):
        self.port.close()

    def _add(self, **overrides) -> str:
        chunk = {
            "tenant_id": "t1",
            "scope": "project",
            "scope_id": "p1",
            "title": "标题",
            "source": "doc.md",
            "content": "内容",
        }
        chunk.update(overrides)
        return self.port.add(chunk)

    # 1. 未审核的块不参与检索
    def test_pending_chunk_is_not_searchable(self):
        chunk_id = self._add(content="SQLite 知识库")
        self.assertEqual(self.port.search("SQLite", tenant_id="t1"), [])
        # 但按 id 回取仍可拿到，供审核 / 引用复核使用
        chunk = self.port.get_chunk(chunk_id)
        self.assertIsNotNone(chunk)
        self.assertEqual(chunk["review_status"], "pending")

    # 2. approve 后可检索，且返回字段齐全
    def test_approved_chunk_is_searchable_with_required_fields(self):
        content = "SQLite 是嵌入式数据库"
        chunk_id = self._add(content=content, title="SQLite 指南")
        self.assertTrue(self.port.approve(chunk_id, reviewer="alice"))

        hits = self.port.search("SQLite", tenant_id="t1")
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        for field in ("id", "content", "title", "source",
                      "content_hash", "review_status", "score"):
            self.assertIn(field, hit)
        self.assertEqual(hit["id"], chunk_id)
        self.assertEqual(hit["content"], content)
        self.assertEqual(hit["title"], "SQLite 指南")
        self.assertEqual(hit["source"], "doc.md")
        self.assertEqual(hit["content_hash"], _sha(content))
        self.assertEqual(hit["review_status"], "approved")

    # 3. get_chunk 命中 / 归档后返回 None
    def test_get_chunk_and_archive(self):
        chunk_id = self._add(content="可回取的正文")
        self.assertIsNotNone(self.port.get_chunk(chunk_id))
        self.assertIsNone(self.port.get_chunk(chunk_id, tenant_id="t2"))

        self.assertTrue(self.port.archive(chunk_id))
        self.assertIsNone(self.port.get_chunk(chunk_id))
        # 归档幂等；已归档的块不能 approve
        self.assertTrue(self.port.archive(chunk_id))
        self.assertFalse(self.port.approve(chunk_id))
        self.assertFalse(self.port.archive("不存在的块"))

    # 4. 租户隔离
    def test_tenant_isolation(self):
        c1 = self._add(content="共同关键词 alpha", tenant_id="t1")
        c2 = self._add(content="共同关键词 alpha", tenant_id="t2")
        self.assertTrue(self.port.approve(c1))
        self.assertTrue(self.port.approve(c2))

        t1_ids = [hit["id"] for hit in self.port.search("alpha", tenant_id="t1")]
        t2_ids = [hit["id"] for hit in self.port.search("alpha", tenant_id="t2")]
        self.assertEqual(t1_ids, [c1])
        self.assertEqual(t2_ids, [c2])
        self.assertEqual(self.port.search("alpha", tenant_id="t3"), [])
        # 指定其他租户时，按 id 也取不到
        self.assertIsNone(self.port.get_chunk(c1, tenant_id="t2"))

    # 5. scope / scope_id 过滤
    def test_scope_filter(self):
        c_p1 = self._add(content="范围关键词 beta", scope="project", scope_id="p1")
        c_p2 = self._add(content="范围关键词 beta", scope="project", scope_id="p2")
        c_platform = self._add(content="范围关键词 beta", scope="platform", scope_id="")
        for chunk_id in (c_p1, c_p2, c_platform):
            self.assertTrue(self.port.approve(chunk_id))

        project_ids = {hit["id"] for hit in self.port.search(
            "beta", tenant_id="t1", scope="project")}
        self.assertEqual(project_ids, {c_p1, c_p2})

        p1_ids = [hit["id"] for hit in self.port.search(
            "beta", tenant_id="t1", scope="project", scope_id="p1")]
        self.assertEqual(p1_ids, [c_p1])

        platform_ids = [hit["id"] for hit in self.port.search(
            "beta", tenant_id="t1", scope="platform")]
        self.assertEqual(platform_ids, [c_platform])

    # 6. 缺 content / source 必须报 ValueError
    def test_missing_content_or_source_raises(self):
        with self.assertRaises(ValueError):
            self.port.add({"source": "doc.md", "content": ""})
        with self.assertRaises(ValueError):
            self.port.add({"source": "doc.md", "content": "   "})
        with self.assertRaises(ValueError):
            self.port.add({"content": "有正文但没有来源"})
        with self.assertRaises(ValueError):
            self.port.add({"source": "  ", "content": "有正文但没有来源"})
        # 校验失败不应留下半条记录
        self.assertEqual(self.port.search("", tenant_id="t1"), [])

    # 7. top_k 限制与排序：标题命中优先，其次正文命中次数
    def test_top_k_and_title_first_ordering(self):
        c_title = self._add(title="SQLite 指南", content="基础内容")
        c_body_many = self._add(title="其他", content="SQLite " * 3)
        c_body_one = self._add(title="其他", content="SQLite")
        for chunk_id in (c_title, c_body_many, c_body_one):
            self.assertTrue(self.port.approve(chunk_id))

        hits = self.port.search("SQLite", top_k=10, tenant_id="t1")
        self.assertEqual([hit["id"] for hit in hits],
                         [c_title, c_body_many, c_body_one])
        self.assertGreater(hits[0]["score"], hits[1]["score"])

        limited = self.port.search("SQLite", top_k=2, tenant_id="t1")
        self.assertEqual([hit["id"] for hit in limited], [c_title, c_body_many])
        self.assertEqual(len(self.port.search("SQLite", top_k=1, tenant_id="t1")), 1)
        self.assertEqual(self.port.search("SQLite", top_k=0, tenant_id="t1"), [])

    # 8. content_hash 稳定：同内容同指纹，改内容指纹变
    def test_content_hash_stability(self):
        c1 = self._add(content="稳定内容")
        c2 = self._add(content="稳定内容", title="另一个标题")
        h1 = self.port.get_chunk(c1)["content_hash"]
        h2 = self.port.get_chunk(c2)["content_hash"]
        self.assertEqual(h1, _sha("稳定内容"))
        self.assertEqual(h1, h2)

        self.port.add({"id": c1, "tenant_id": "t1", "source": "doc.md",
                       "content": "变更后的内容"})
        h3 = self.port.get_chunk(c1)["content_hash"]
        self.assertNotEqual(h3, h1)
        self.assertEqual(h3, _sha("变更后的内容"))

    # 9. query 为空时按 updated_at 倒序
    def test_empty_query_orders_by_recency(self):
        older = self._add(content="较早的块", review_status="approved")
        time.sleep(0.01)
        newer = self._add(content="较新的块", review_status="approved")
        hits = self.port.search("", tenant_id="t1")
        self.assertEqual([hit["id"] for hit in hits], [newer, older])

    # 10. 中文按子串、英文大小写不敏感
    def test_chinese_substring_and_english_case_insensitive(self):
        c_zh = self._add(content="企业知识库支持检索", review_status="approved")
        self._add(content="与主题无关的内容", title="无关",
                  review_status="approved")
        c_en = self._add(content="SQLite 数据库", review_status="approved")

        self.assertEqual([hit["id"] for hit in self.port.search(
            "知识库", tenant_id="t1")], [c_zh])
        self.assertEqual([hit["id"] for hit in self.port.search(
            "sqlite", tenant_id="t1")], [c_en])

    # 11. 同内容重写不改变审核状态；内容变化回到待审
    def test_update_keeps_review_state_only_if_content_unchanged(self):
        chunk_id = self._add(content="同一内容")
        self.assertTrue(self.port.approve(chunk_id))

        self.port.add({"id": chunk_id, "tenant_id": "t1", "source": "doc.md",
                       "content": "同一内容", "title": "新标题"})
        self.assertEqual(self.port.get_chunk(chunk_id)["review_status"], "approved")

        self.port.add({"id": chunk_id, "tenant_id": "t1", "source": "doc.md",
                       "content": "改了内容"})
        self.assertEqual(self.port.get_chunk(chunk_id)["review_status"], "pending")
        self.assertEqual(self.port.search("同一内容", tenant_id="t1"), [])


if __name__ == "__main__":
    unittest.main()
