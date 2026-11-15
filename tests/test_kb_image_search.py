# -*- coding: utf-8 -*-
"""kb_image_search：入库计划与变更行替换（不加载模型、不访问真实 LanceDB）。"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb import image_search as kb_img


class TestPlanImageIngest(unittest.TestCase):
    def test_new_images(self):
        plan = kb_img.plan_image_ingest({}, [("a.png", 1), ("b.png", 2)])
        self.assertEqual(plan, {
            "new": ["a.png", "b.png"],
            "changed": [],
            "skipped": [],
            "duplicates": [],
        })

    def test_changed_images(self):
        plan = kb_img.plan_image_ingest({"a.png": 1}, [("a.png", 2)])
        self.assertEqual(plan["changed"], ["a.png"])
        self.assertEqual(plan["new"], [])
        self.assertEqual(plan["skipped"], [])

    def test_skipped_images(self):
        plan = kb_img.plan_image_ingest({"a.png": 1, "b.png": 2},
                                        [("a.png", 1), ("b.png", 2)])
        self.assertEqual(plan["skipped"], ["a.png", "b.png"])
        self.assertEqual(plan["new"], [])
        self.assertEqual(plan["changed"], [])

    def test_batch_duplicates_counted_once(self):
        plan = kb_img.plan_image_ingest(
            {"a.png": 1},
            [("a.png", 2), ("a.png", 2), ("b.png", 3), ("b.png", 3)],
        )
        self.assertEqual(plan["changed"], ["a.png"])
        self.assertEqual(plan["new"], ["b.png"])
        self.assertEqual(plan["duplicates"], ["a.png", "b.png"])
        self.assertEqual(plan["skipped"], [])


class _Vector:
    def tolist(self):
        return [0.0, 0.0]


class _FakeColumn:
    def __init__(self, values):
        self._values = values

    def to_pylist(self):
        return list(self._values)


class _FakeArrow:
    def __init__(self, data):
        self._data = data

    def column(self, name):
        return _FakeColumn(self._data[name])


class _FakeLanceTable:
    def __init__(self, rows):
        self._rows = rows

    def to_table(self, columns=None):
        columns = columns or ["image_path", "mtime_ns"]
        return _FakeArrow({name: [row[name] for row in self._rows] for name in columns})


class FakeImageTable:
    """记录 delete/add 顺序，并按 image_path 模拟行替换。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.operations = []

    def delete(self, where):
        self.operations.append(("delete", where))
        if where == "true":
            removed = len(self.rows)
            self.rows = []
            return SimpleNamespace(num_deleted_rows=removed)
        prefix = "image_path = '"
        path = where[len(prefix):-1].replace("''", "'")
        removed = sum(1 for row in self.rows if row["image_path"] == path)
        self.rows = [row for row in self.rows if row["image_path"] != path]
        return SimpleNamespace(num_deleted_rows=removed)

    def add(self, rows):
        self.operations.append(("add", [row["image_path"] for row in rows]))
        self.rows.extend(rows)

    def to_lance(self):
        return _FakeLanceTable(self.rows)

    def __len__(self):
        return len(self.rows)


class TestIngestImages(unittest.TestCase):
    @staticmethod
    def _fake_embed(paths, batch_size=None):
        return [(path, _Vector()) for path in paths]

    def _run_ingest(self, table, scan_dir):
        with mock.patch.object(kb_img, "get_image_table", return_value=table), \
             mock.patch.object(kb_img, "image_count",
                               side_effect=lambda: len(table.rows)), \
             mock.patch.object(kb_img, "embed_images", side_effect=self._fake_embed):
            return kb_img.ingest_images(scan_dir=scan_dir, batch_size=8)

    def test_changed_image_deleted_before_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            changed = os.path.join(tmp, "changed.png")
            new = os.path.join(tmp, "new.png")
            for path in (changed, new):
                with open(path, "wb") as fh:
                    fh.write(b"not-a-real-image")
            table = FakeImageTable([
                {"image_path": changed, "mtime_ns": os.stat(changed).st_mtime_ns - 1},
            ])

            output = self._run_ingest(table, tmp)

            self.assertIn("新增/更新 2 张", output)
            expected_delete = ("delete", f"image_path = '{changed}'")
            self.assertIn(expected_delete, table.operations)
            self.assertLess(table.operations.index(expected_delete),
                            next(i for i, op in enumerate(table.operations)
                                 if op[0] == "add"))
            self.assertEqual(len(table.rows), 2)
            self.assertEqual({row["image_path"] for row in table.rows}, {changed, new})
            changed_row = next(row for row in table.rows
                               if row["image_path"] == changed)
            self.assertEqual(changed_row["mtime_ns"], os.stat(changed).st_mtime_ns)

    def test_unchanged_image_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "same.png")
            with open(path, "wb") as fh:
                fh.write(b"not-a-real-image")
            table = FakeImageTable([
                {"image_path": path, "mtime_ns": os.stat(path).st_mtime_ns},
            ])

            output = self._run_ingest(table, tmp)

            self.assertIn("无新增/变更图片", output)
            self.assertEqual(table.operations, [])

    def test_delete_where_escapes_single_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "it's.png")
            with open(path, "wb") as fh:
                fh.write(b"not-a-real-image")
            table = FakeImageTable([
                {"image_path": path, "mtime_ns": os.stat(path).st_mtime_ns - 1},
            ])

            self._run_ingest(table, tmp)

            escaped = path.replace("'", "''")
            self.assertIn(("delete", f"image_path = '{escaped}'"), table.operations)


if __name__ == "__main__":
    unittest.main()
