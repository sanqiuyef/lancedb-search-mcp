# -*- coding: utf-8 -*-

import os
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from knowledge_graph_desktop import ALL_BASES_SCOPE, ContentEdgeItem, KnowledgeBrowserWindow


class MiniExtractor:
    name = "mini"
    version = "1"

    def extract(self, chunks):
        return {
            "entities": [{
                "label": "CNN-LSTM", "kind": "method", "chunk_index": 0, "confidence": 0.9,
            }],
            "claims": [{
                "text": "The paper evaluates CNN-LSTM.", "chunk_index": 1,
                "confidence": 0.8, "entities": ["CNN-LSTM"],
            }],
            "relations": [],
        }


def temporary_db_path():
    handle, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(handle)
    os.remove(path)
    return path


def remove_sqlite(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def wait_until(predicate, timeout_ms=5000):
    loop = QEventLoop()
    state = {"matched": False}

    def poll():
        if predicate():
            state["matched"] = True
            loop.quit()
        else:
            QTimer.singleShot(10, poll)

    QTimer.singleShot(0, poll)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return state["matched"]


class DesktopAsyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_document_chunks_complete_and_full_text_is_lazy(self):
        relation_path = temporary_db_path()
        content_graph_path = temporary_db_path()
        window = KnowledgeBrowserWindow("kb-config.json", relation_path, content_graph_path)
        try:
            self.assertTrue(wait_until(lambda: bool(window.documents)))
            window.kb_combo.setCurrentIndex(window.kb_combo.findData("project-小论文（北松区）"))
            self.assertTrue(wait_until(
                lambda: window.current_base() == "project-小论文（北松区）"
                and any("Hybrid CNN-LSTM" in item["source"] for item in window.visible_documents)
            ))
            row = next(index for index, item in enumerate(window.visible_documents)
                       if "Hybrid CNN-LSTM" in item["source"])
            started = time.perf_counter()
            window.document_table.selectRow(row)
            self.assertTrue(wait_until(lambda: len(window.current_chunks) == 162))
            self.assertLess(time.perf_counter() - started, 5.0)
            self.assertEqual("切换到“全文”标签后按需生成。", window.full_text.toPlainText())
            window.content_tabs.setCurrentIndex(1)
            self.assertGreater(len(window.full_text.toPlainText()), 100_000)
            self.assertFalse(window.last_error)
        finally:
            window.close()
            window.thread_pool.waitForDone(5000)
            window.chunk_pool.waitForDone(5000)
            remove_sqlite(relation_path)
            remove_sqlite(content_graph_path)

    def test_content_graph_canvas_adapts_to_window_size(self):
        relation_path = temporary_db_path()
        content_graph_path = temporary_db_path()
        window = KnowledgeBrowserWindow("kb-config.json", relation_path, content_graph_path)
        try:
            window.resize(1600, 940)
            window.show()
            window.main_tabs.setCurrentIndex(2)
            self.assertTrue(wait_until(lambda: window.content_graph_view.height() > 650))
            self.assertGreater(window.content_graph_view.width(), 1000)

            window.resize(1100, 700)
            self.assertTrue(wait_until(lambda: 430 < window.content_graph_view.height() < 650))
            self.assertGreater(window.content_graph_view.width(), 650)
        finally:
            window.close()
            window.thread_pool.waitForDone(5000)
            window.chunk_pool.waitForDone(5000)
            remove_sqlite(relation_path)
            remove_sqlite(content_graph_path)

    def test_all_knowledge_bases_graph_and_cross_base_jump(self):
        relation_path = temporary_db_path()
        content_graph_path = temporary_db_path()
        window = KnowledgeBrowserWindow("kb-config.json", relation_path, content_graph_path)
        try:
            self.assertTrue(wait_until(lambda: bool(window.documents)))
            window.graph_scope.setCurrentIndex(window.graph_scope.findData(ALL_BASES_SCOPE))
            window.main_tabs.setCurrentIndex(1)
            self.assertTrue(wait_until(lambda: window.graph_loaded_for == ALL_BASES_SCOPE, 10_000))
            self.assertEqual(134, len(window.graph["nodes"]))
            self.assertEqual(
                {"project-通用", "project-小论文（北松区）", "project-BIMbase"},
                set(window.graph["knowledge_bases"]),
            )
            target = next(item for item in window.graph["nodes"] if item["knowledge_base"] == "project-BIMbase")
            window.node_items[target["id"]].setSelected(True)
            window.jump_from_graph()
            self.assertTrue(wait_until(
                lambda: window.current_base() == "project-BIMbase"
                and window.current_document is not None
                and window.current_document["source"] == target["source"],
                5_000,
            ))
        finally:
            window.close()
            window.thread_pool.waitForDone(5000)
            window.chunk_pool.waitForDone(5000)
            remove_sqlite(relation_path)
            remove_sqlite(content_graph_path)

    def test_content_graph_opens_evidence_chunk(self):
        relation_path = temporary_db_path()
        content_graph_path = temporary_db_path()
        window = KnowledgeBrowserWindow("kb-config.json", relation_path, content_graph_path)
        try:
            self.assertTrue(wait_until(lambda: bool(window.documents)))
            window.main_tabs.setCurrentIndex(2)
            self.assertGreater(len(window.content_graph_scene.items()), 0)
            self.assertIn("上方", window.content_graph_detail.toPlainText())
            window.content_graph_kb_combo.setCurrentIndex(
                window.content_graph_kb_combo.findData("project-小论文（北松区）")
            )
            self.assertTrue(wait_until(
                lambda: window.current_base() == "project-小论文（北松区）"
                and any(
                    "Hybrid CNN-LSTM" in window.content_graph_document_combo.itemText(index)
                    for index in range(window.content_graph_document_combo.count())
                )
            ))
            target_index = next(
                index for index in range(window.content_graph_document_combo.count())
                if "Hybrid CNN-LSTM" in window.content_graph_document_combo.itemText(index)
            )
            window.content_graph_document_combo.setCurrentIndex(target_index)
            self.assertTrue(wait_until(lambda: len(window.current_chunks) == 162))
            document = window.current_document
            window.content_graph_store.index_document(
                document["knowledge_base"], document["source"], window.current_chunks[:2], MiniExtractor()
            )
            window.load_content_graph()
            self.assertTrue(wait_until(lambda: bool(window.content_graph.get("document_id"))))
            self.assertTrue({"document", "chunk", "method", "claim"}.issubset(
                {node["kind"] for node in window.content_graph["nodes"]}
            ))
            edge_item = next(
                item for item in window.content_graph_scene.items()
                if isinstance(item, ContentEdgeItem) and item.edge["relation_type"] == "asserts"
            )
            edge_item.setSelected(True)
            self.assertEqual(1, window.current_content_evidence["chunk_index"])
            window.locate_content_evidence()
            self.assertEqual(0, window.main_tabs.currentIndex())
            self.assertEqual(2, window.content_tabs.currentIndex())
            self.assertEqual(1, window.current_chunks[window.chunk_list.currentRow()]["chunk_index"])
        finally:
            window.close()
            window.thread_pool.waitForDone(5000)
            window.chunk_pool.waitForDone(5000)
            remove_sqlite(relation_path)
            remove_sqlite(content_graph_path)


if __name__ == "__main__":
    unittest.main()
