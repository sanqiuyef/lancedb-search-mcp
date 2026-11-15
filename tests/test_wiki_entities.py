# -*- coding: utf-8 -*-
"""实体规范化/候选召回/显式合并、关系白名单与邻域 BFS。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations
from kb.wiki.entity_resolver import (
    EntityAmbiguityError,
    EntityMergeError,
    find_candidates,
    get_merge_target,
    merge_entity,
    normalize_name,
    upsert_entity,
)
from kb.wiki.repositories import entities as entities_repo
from kb.wiki.repositories import relations as relations_repo


class WikiEntityTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.config = WikiConfig(
            enabled=True,
            vault_path=os.path.join(self.root, "vault"),
            db_path=os.path.join(self.root, "db", "wiki.sqlite3"),
        )
        self.conn = connect(self.config.db_path, self.config)
        self.addCleanup(self.conn.close)
        run_migrations(self.conn)

    def insert_page(self, page_id="page-1", slug="page-1"):
        self.conn.execute(
            "INSERT INTO pages (page_id, slug, title, page_type, relative_path) "
            "VALUES (?, ?, ?, 'concept', ?)",
            (
                page_id,
                slug,
                f"Title {slug}",
                f"wiki-维基/concepts-概念/{slug}.md",
            ),
        )
        return page_id

    def aliases(self, entity_id):
        return entities_repo.find_aliases_by_entity(self.conn, entity_id)

    def alias_pairs(self, entity_id):
        return {(row.alias, str(row.alias_type)) for row in self.aliases(entity_id)}


class TestNormalizeName(unittest.TestCase):
    def test_casefold_punctuation_whitespace_and_fullwidth(self):
        self.assertEqual(normalize_name("  New-York  City "), "newyorkcity")
        self.assertEqual(normalize_name("ＡＩ 智能体"), "ai智能体")
        self.assertEqual(normalize_name("C++"), "c++")
        self.assertEqual(normalize_name("LanceDB / Lance 数据库"), "lancedblance数据库")
        self.assertEqual(
            normalize_name("RAG（检索增强生成）"), "rag检索增强生成"
        )

    def test_empty_or_punctuation_only_rejected(self):
        for value in ("", "   ", "!!!", "---"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_name(value)


class TestUpsertAndCandidates(WikiEntityTestBase):
    def test_upsert_creates_entity_alias_and_page_link(self):
        page_id = self.insert_page()
        entity = upsert_entity(
            self.conn,
            "LanceDB",
            entity_type="software",
            description="向量数据库",
            confidence=0.9,
            status="accepted",
            aliases=["Lance 数据库", "Lance"],
            page_id=page_id,
            role="tool",
            page_confidence=0.8,
            created_by="agent",
        )
        self.assertEqual(len(entity.entity_id), 26)
        self.assertEqual(entity.canonical_name, "LanceDB")
        self.assertEqual(entity.normalized_name, "lancedb")
        self.assertEqual(entity.entity_type, "software")
        self.assertEqual(str(entity.status), "accepted")
        self.assertIn(("Lance 数据库", "alias"), self.alias_pairs(entity.entity_id))
        page_entities = entities_repo.get_page_entities(self.conn, page_id)
        self.assertEqual(len(page_entities), 1)
        self.assertEqual(page_entities[0].entity_id, entity.entity_id)
        self.assertEqual(page_entities[0].role, "tool")
        self.assertAlmostEqual(page_entities[0].confidence, 0.8)

    def test_upsert_reuses_exact_identity_and_adds_alias(self):
        first = upsert_entity(self.conn, "LanceDB")
        second = upsert_entity(self.conn, "LanceDB", aliases=["Lance"])
        self.assertEqual(first.entity_id, second.entity_id)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1
        )
        self.assertIn(("Lance", "alias"), self.alias_pairs(first.entity_id))

    def test_upsert_resolves_by_alias(self):
        first = upsert_entity(self.conn, "LanceDB", aliases=["Lance 数据库"])
        second = upsert_entity(self.conn, "lance 数据库")
        self.assertEqual(second.entity_id, first.entity_id)

    def test_similar_names_are_not_auto_merged(self):
        urban_flood = upsert_entity(self.conn, "城市洪涝")
        urban_waterlog = upsert_entity(self.conn, "城市内涝")
        flood_model = upsert_entity(self.conn, "城市洪涝模型")
        self.assertEqual(
            len({urban_flood.entity_id, urban_waterlog.entity_id, flood_model.entity_id}),
            3,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 3
        )

    def test_find_candidates_exact_alias_like_and_type(self):
        entity = upsert_entity(
            self.conn,
            "LanceDB",
            entity_type="software",
            aliases=["Lance 数据库"],
        )
        other = upsert_entity(self.conn, "Lance", entity_type="person")
        upsert_entity(self.conn, "Vector Database", entity_type="software")

        self.assertEqual(
            [item.entity_id for item in find_candidates(self.conn, "lancedb")],
            [entity.entity_id],
        )
        self.assertEqual(
            [item.entity_id for item in find_candidates(self.conn, "Lance 数据库")],
            [entity.entity_id],
        )
        by_type = find_candidates(self.conn, "Lance", entity_type="software")
        self.assertEqual([item.entity_id for item in by_type], [entity.entity_id])
        self.assertNotIn(other.entity_id, {item.entity_id for item in by_type})
        like = find_candidates(self.conn, "数据库")
        self.assertIn(entity.entity_id, {item.entity_id for item in like})

    def test_ambiguous_exact_alias_requires_explicit_merge(self):
        first = upsert_entity(self.conn, "Alpha", aliases=["Shared"])
        second = upsert_entity(self.conn, "Beta")
        # 绕过 upsert 的冲突检查，模拟历史数据里两个实体共用同一别名。
        entities_repo.insert_alias(
            self.conn, second.entity_id, "Shared", normalize_name("Shared")
        )
        with self.assertRaises(EntityAmbiguityError) as ctx:
            upsert_entity(self.conn, "Shared")
        ids = {entity.entity_id for entity in ctx.exception.candidates}
        self.assertEqual(ids, {first.entity_id, second.entity_id})

    def test_upsert_rejects_alias_owned_by_other_entity(self):
        upsert_entity(self.conn, "LanceDB")
        with self.assertRaises(EntityAmbiguityError):
            upsert_entity(self.conn, "Other", aliases=["LanceDB"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1
        )


class TestMergeEntity(WikiEntityTestBase):
    def test_explicit_merge_keeps_source_and_redirects_aliases(self):
        source = upsert_entity(
            self.conn,
            "Lance Database",
            description="source",
            aliases=["Lance 数据库", "Lance Data"],
        )
        target = upsert_entity(self.conn, "LanceDB")

        result = merge_entity(
            self.conn, source.entity_id, target.entity_id, actor="agent"
        )

        self.assertEqual(result.status, "merged")
        self.assertEqual(result.merged_into, target.entity_id)
        self.assertEqual(result.source_status, "archived")
        self.assertEqual(result.redirect["to"], "LanceDB")
        self.assertEqual(
            get_merge_target(self.conn, source.entity_id), target.entity_id
        )

        kept = entities_repo.get_entity(self.conn, source.entity_id)
        self.assertIsNotNone(kept)
        self.assertEqual(str(kept.status), "archived")
        self.assertEqual(kept.canonical_name, "Lance Database")

        target_aliases = self.alias_pairs(target.entity_id)
        self.assertIn(("Lance Database", "redirect"), target_aliases)
        self.assertIn(("Lance 数据库", "alias"), target_aliases)
        self.assertIn(("Lance Data", "alias"), target_aliases)

        # source 别名仍保留（保留 source 记录），target 迁移了别名。
        source_aliases = self.alias_pairs(source.entity_id)
        self.assertIn(("Lance 数据库", "alias"), source_aliases)
        self.assertIn((target.entity_id, "redirect"), source_aliases)

        # 旧名称重新解析时命中 target，而不是已归档的 source。
        candidates = find_candidates(self.conn, "Lance Database")
        self.assertEqual([item.entity_id for item in candidates], [target.entity_id])
        resolved = upsert_entity(self.conn, "Lance Database")
        self.assertEqual(resolved.entity_id, target.entity_id)

    def test_merge_is_idempotent_for_same_target(self):
        source = upsert_entity(self.conn, "A")
        target = upsert_entity(self.conn, "B")
        merge_entity(self.conn, source.entity_id, target.entity_id)
        again = merge_entity(self.conn, source.entity_id, target.entity_id)
        self.assertEqual(again.status, "merged")
        self.assertEqual(again.aliases_migrated, 0)
        self.assertEqual(
            get_merge_target(self.conn, source.entity_id), target.entity_id
        )

    def test_self_merge_rejected(self):
        entity = upsert_entity(self.conn, "A")
        with self.assertRaises(EntityMergeError):
            merge_entity(self.conn, entity.entity_id, entity.entity_id)

    def test_redirect_cycle_rejected(self):
        first = upsert_entity(self.conn, "A")
        second = upsert_entity(self.conn, "B")
        merge_entity(self.conn, first.entity_id, second.entity_id)
        with self.assertRaises(EntityMergeError):
            merge_entity(self.conn, second.entity_id, first.entity_id)
        self.assertIsNone(get_merge_target(self.conn, second.entity_id))
        self.assertEqual(
            str(entities_repo.get_entity(self.conn, second.entity_id).status),
            "proposed",
        )

    def test_merge_missing_entity_rejected(self):
        entity = upsert_entity(self.conn, "A")
        with self.assertRaises(LookupError):
            merge_entity(self.conn, entity.entity_id, "missing")
        with self.assertRaises(LookupError):
            merge_entity(self.conn, "missing", entity.entity_id)


class TestRelationsAndNeighbourhood(WikiEntityTestBase):
    def setUp(self):
        super().setUp()
        self.page_id = self.insert_page("page-1", "page-1")
        self.a = upsert_entity(
            self.conn, "A", entity_type="model", page_id=self.page_id
        )
        self.b = upsert_entity(self.conn, "B", entity_type="method")
        self.c = upsert_entity(self.conn, "C", entity_type="dataset")

    def test_relation_predicate_whitelist_and_default_status(self):
        relation = relations_repo.create_relation(
            self.conn, self.a.entity_id, "uses", self.b.entity_id
        )
        self.assertEqual(str(relation.status), "proposed")
        self.assertIn("uses", relations_repo.PREDICATES)
        with self.assertRaises(ValueError):
            relations_repo.create_relation(
                self.conn, self.a.entity_id, "same_as", self.b.entity_id
            )
        with self.assertRaises(ValueError):
            relations_repo.create_relation(
                self.conn, self.a.entity_id, "uses", self.a.entity_id
            )

    def test_duplicate_relation_returns_existing(self):
        first = relations_repo.create_relation(
            self.conn, self.a.entity_id, "uses", self.b.entity_id
        )
        second = relations_repo.create_relation(
            self.conn,
            self.a.entity_id,
            "uses",
            self.b.entity_id,
            confidence=0.99,
            evidence="different input",
        )
        self.assertEqual(first.relation_id, second.relation_id)
        self.assertEqual(second.confidence, 0.0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 1
        )

    def test_store_relation_unique(self):
        first = relations_repo.create_relation(
            self.conn, self.a.entity_id, "part_of", self.b.entity_id
        )
        second = relations_repo.create_relation(
            self.conn, self.a.entity_id, "part_of", self.b.entity_id
        )
        self.assertEqual(first.relation_id, second.relation_id)

    def test_get_related_depth_and_page_seed(self):
        relations_repo.create_relation(
            self.conn, self.a.entity_id, "uses", self.b.entity_id
        )
        relations_repo.create_relation(
            self.conn, self.b.entity_id, "evaluated_on", self.c.entity_id
        )
        depth_one = relations_repo.get_related(
            self.conn, page_id=self.page_id, depth=1, limit=30
        )
        depth_two = relations_repo.get_related(
            self.conn, page_id=self.page_id, depth=2, limit=30
        )
        one_names = {item.entity.canonical_name for item in depth_one}
        two_names = {item.entity.canonical_name for item in depth_two}
        self.assertEqual(one_names, {"B"})
        self.assertEqual(two_names, {"B", "C"})
        self.assertEqual(
            {item.depth for item in depth_two if item.entity.canonical_name == "C"},
            {2},
        )
        via_entity = relations_repo.get_related(
            self.conn, entity_id=self.a.entity_id, depth=1
        )
        self.assertEqual([item.entity.canonical_name for item in via_entity], ["B"])
        with self.assertRaises(ValueError):
            relations_repo.get_related(self.conn, page_id=self.page_id, depth=3)
        with self.assertRaises(ValueError):
            relations_repo.get_related(self.conn, depth=1)

    def test_get_related_excludes_archived_and_rejected(self):
        archived = upsert_entity(self.conn, "Archived")
        entities_repo.update_entity(self.conn, archived.entity_id, status="archived")
        relations_repo.create_relation(
            self.conn, self.a.entity_id, "supports", archived.entity_id
        )
        rejected = upsert_entity(self.conn, "Rejected")
        relations_repo.update_relation_status(
            self.conn,
            relations_repo.create_relation(
                self.conn, self.a.entity_id, "contradicts", rejected.entity_id
            ).relation_id,
            "rejected",
        )
        related = relations_repo.get_related(
            self.conn, entity_id=self.a.entity_id, depth=1
        )
        self.assertEqual(related, [])

    def test_invalid_status_and_predicate_rejected(self):
        with self.assertRaises(ValueError):
            relations_repo.create_relation(
                self.conn, self.a.entity_id, "uses", self.b.entity_id, status="bogus"
            )
        with self.assertRaises(ValueError):
            relations_repo.list_relations(self.conn, predicate="bogus")
        with self.assertRaises(ValueError):
            relations_repo.update_relation_status(self.conn, "x", "bogus")


if __name__ == "__main__":
    unittest.main()
