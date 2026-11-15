# -*- coding: utf-8 -*-
"""链接索引：出链/反链/断链、slug/title/alias 解析与改名后的旧链接。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.wiki import link_index
from kb.wiki.config import WikiConfig
from kb.wiki.db import connect, run_migrations
from kb.wiki.markdown import render_frontmatter
from kb.wiki.page_store import PageDraft, PageStore
from kb.wiki.repositories import pages as pages_repo


class WikiLinkTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(self.vault)
        self.config = WikiConfig(
            enabled=True,
            vault_path=self.vault,
            db_path=os.path.join(self.root, "db", "wiki.sqlite3"),
        )
        self.conn = connect(self.config.db_path, self.config)
        self.addCleanup(self.conn.close)
        run_migrations(self.conn)
        self.store = PageStore(self.config, self.conn)

    def create_page(
        self,
        slug,
        title=None,
        body="",
        status="published",
        aliases=None,
        page_type="concept",
    ):
        return self.store.create_page(
            PageDraft(
                title=title or slug,
                page_type=page_type,
                summary=f"{title or slug} 的摘要。",
                body=body,
                slug=slug,
                aliases=aliases or [],
                status=status,
            ),
            commit=False,
        )

    def update_body(self, page, body):
        return self.store.update_page(
            page.page_id,
            body,
            expected_revision_id=page.current_revision_id,
            commit=False,
        )

    def path(self, page):
        return os.path.join(
            self.vault, *page.relative_path.replace("\\", "/").split("/")
        )


class TestLinkIndex(WikiLinkTestBase):
    def test_outgoing_backlinks_and_broken(self):
        target = self.create_page("target", title="Target Page")
        source = self.create_page(
            "source", body="[[target]] [[target|别名]] [[missing]]\n"
        )

        links = link_index.outgoing_links(self.conn, source.page_id)
        self.assertEqual(len(links), 2)
        by_slug = {link.target_slug: link for link in links}
        self.assertEqual(by_slug["target"].target_page_id, target.page_id)
        self.assertEqual(by_slug["target"].display_text, "target")
        self.assertIsNone(by_slug["missing"].target_page_id)

        backlinks = link_index.backlinks(self.conn, target.page_id)
        self.assertEqual(len(backlinks), 1)
        self.assertEqual(backlinks[0].source_page_id, source.page_id)
        self.assertEqual(
            [link.target_slug for link in link_index.broken_links(self.conn)],
            ["missing"],
        )

        view = self.store.read_page(source.page_id)
        self.assertEqual(len(view.links), 2)
        self.assertEqual(view.backlinks, [])

    def test_slug_title_alias_case_and_anchor_resolution(self):
        target = self.create_page(
            "b", title="B Title", aliases=["Bee", "蜜蜂"]
        )
        source = self.create_page(
            "source", body="[[B]] [[B Title]] [[Bee]] [[蜜蜂]]\n"
        )
        links = link_index.outgoing_links(self.conn, source.page_id)
        self.assertEqual(len(links), 4)
        self.assertTrue(
            all(link.target_page_id == target.page_id for link in links)
        )

        anchor_links = link_index.rebuild_page_links(
            self.conn,
            source.page_id,
            "[[B#Section|小节]]\n",
            config=self.config,
        )
        self.assertEqual(len(anchor_links), 1)
        self.assertEqual(anchor_links[0].target_slug, "B")
        self.assertEqual(anchor_links[0].display_text, "小节")
        self.assertEqual(anchor_links[0].target_page_id, target.page_id)

    def test_alias_resolution_needs_config(self):
        target = self.create_page("b", aliases=["Bee"])
        source = self.create_page("source", body="[[Bee]]\n")
        self.assertEqual(
            link_index.outgoing_links(self.conn, source.page_id)[0].target_page_id,
            target.page_id,
        )

        links = link_index.rebuild_page_links(
            self.conn, source.page_id, "[[Bee]]\n", config=None
        )
        self.assertIsNone(links[0].target_page_id)

        links = link_index.rebuild_page_links(
            self.conn, source.page_id, "[[Bee]]\n", config=self.config
        )
        self.assertEqual(links[0].target_page_id, target.page_id)

    def test_slug_wins_over_title_and_alias(self):
        slug_page = self.create_page("shared", title="Shared Slug Page")
        self.create_page("other", title="shared", aliases=["SHARED"])
        source = self.create_page("source", body="[[shared]]\n")
        link = link_index.outgoing_links(self.conn, source.page_id)[0]
        self.assertEqual(link.target_page_id, slug_page.page_id)

    def test_case_insensitive_dedup(self):
        target = self.create_page("target")
        source = self.create_page("source", body="[[Target]] [[target]]\n")
        links = link_index.outgoing_links(self.conn, source.page_id)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_slug, "Target")
        self.assertEqual(links[0].target_page_id, target.page_id)

    def test_rename_turns_old_link_into_broken_link(self):
        target = self.create_page("old", title="Old")
        source = self.create_page("source", body="[[old]]\n")
        self.assertEqual(
            link_index.outgoing_links(self.conn, source.page_id)[0].target_page_id,
            target.page_id,
        )

        old_path = self.path(target)
        new_path = os.path.join(os.path.dirname(old_path), "new.md")
        os.replace(old_path, new_path)
        pages_repo.update_page(
            self.conn,
            target.page_id,
            slug="new",
            title="New",
            relative_path="wiki-维基/concepts-概念/new.md",
        )

        links = link_index.rebuild_page_links(
            self.conn, source.page_id, "[[old]]\n", config=self.config
        )
        self.assertIsNone(links[0].target_page_id)
        self.assertEqual(links[0].target_slug, "old")
        self.assertEqual(link_index.backlinks(self.conn, target.page_id), [])
        self.assertEqual(
            [link.target_slug for link in link_index.broken_links(self.conn)],
            ["old"],
        )

        links = link_index.rebuild_page_links(
            self.conn, source.page_id, "[[new]]\n", config=self.config
        )
        self.assertEqual(links[0].target_page_id, target.page_id)

    def test_update_rebuilds_links_and_draft_clears_them(self):
        target = self.create_page("target")
        source = self.create_page("source", body="[[target]]\n")
        self.assertEqual(
            len(link_index.outgoing_links(self.conn, source.page_id)), 1
        )

        result = self.update_body(source, "[[target]] [[missing]]\n")
        self.assertEqual(result.status, "updated")
        links = link_index.outgoing_links(self.conn, source.page_id)
        self.assertEqual(len(links), 2)
        self.assertEqual(
            sorted(link.target_slug for link in links), ["missing", "target"]
        )

        view = self.store.read_page(source.page_id)
        frontmatter = dict(view.frontmatter)
        frontmatter["status"] = "draft"
        result = self.store.update_page(
            source.page_id,
            render_frontmatter(frontmatter, view.body),
            expected_revision_id=view.page.current_revision_id,
            commit=False,
        )
        self.assertEqual(result.status, "updated")
        self.assertEqual(
            link_index.outgoing_links(self.conn, source.page_id), []
        )
        self.assertEqual(link_index.backlinks(self.conn, target.page_id), [])

    def test_publishing_draft_creates_links(self):
        target = self.create_page("target")
        source = self.create_page("source", body="[[target]]\n", status="draft")
        self.assertEqual(
            link_index.outgoing_links(self.conn, source.page_id), []
        )
        view = self.store.read_page(source.page_id)
        frontmatter = dict(view.frontmatter)
        frontmatter["status"] = "published"
        self.store.update_page(
            source.page_id,
            render_frontmatter(frontmatter, view.body),
            expected_revision_id=view.page.current_revision_id,
            commit=False,
        )
        links = link_index.outgoing_links(self.conn, source.page_id)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_page_id, target.page_id)

    def test_self_link_is_allowed(self):
        page = self.create_page("self", body="[[self]]\n")
        links = link_index.outgoing_links(self.conn, page.page_id)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_page_id, page.page_id)
        self.assertEqual(
            [link.source_page_id for link in link_index.backlinks(self.conn, page.page_id)],
            [page.page_id],
        )

    def test_rebuild_unknown_page_raises(self):
        with self.assertRaises(link_index.LinkPageNotFoundError):
            link_index.rebuild_page_links(
                self.conn, "missing-page", "[[x]]", config=self.config
            )


class TestParseWikiLinks(unittest.TestCase):
    def test_iter_links(self):
        body = (
            "A [[Target]] B [[Other|Alias]] C [[Third#Section|小节]] "
            "[[ | 空 ]] [[#only-anchor]]"
        )
        self.assertEqual(
            link_index.iter_wiki_links(body),
            [
                ("Target", "Target"),
                ("Other", "Alias"),
                ("Third", "小节"),
            ],
        )

    def test_iter_links_rejects_non_string(self):
        with self.assertRaises(TypeError):
            link_index.iter_wiki_links(None)


if __name__ == "__main__":
    unittest.main()
