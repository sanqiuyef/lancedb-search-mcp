# -*- coding: utf-8 -*-
"""kb_image_search：入库计划、变更行替换与过滤转义（不加载模型、不访问真实 LanceDB）。"""

import os
import re
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
        plan = kb_img.plan_image_ingest({"a.png": [1]}, [("a.png", 2)])
        self.assertEqual(plan["changed"], ["a.png"])
        self.assertEqual(plan["new"], [])
        self.assertEqual(plan["skipped"], [])

    def test_skipped_images(self):
        plan = kb_img.plan_image_ingest({"a.png": [1], "b.png": [2]},
                                        [("a.png", 1), ("b.png", 2)])
        self.assertEqual(plan["skipped"], ["a.png", "b.png"])
        self.assertEqual(plan["new"], [])
        self.assertEqual(plan["changed"], [])

    def test_duplicate_rows_force_changed_even_with_same_mtime(self):
        plan = kb_img.plan_image_ingest(
            {"a.png": [5, 5]},
            [("a.png", 5)],
        )
        self.assertEqual(plan["changed"], ["a.png"])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["new"], [])

    def test_batch_duplicates_counted_once(self):
        plan = kb_img.plan_image_ingest(
            {"a.png": [1]},
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

    @property
    def column_names(self):
        return list(self._data)

    def column(self, name):
        return _FakeColumn(self._data[name])


class _FakeLanceTable:
    def __init__(self, table):
        self._table = table

    def to_table(self, columns=None, with_row_id=False):
        if with_row_id and not self._table.supports_rowid:
            raise TypeError("with_row_id not supported")
        columns = list(columns or ["image_path", "mtime_ns"])
        data = {name: [row[name] for row in self._table.rows]
                for name in columns}
        if with_row_id:
            data["_rowid"] = [row["_rowid"] for row in self._table.rows]
        return _FakeArrow(data)


def _unquote(value):
    return value.replace("''", "'")


class FakeImageTable:
    """记录 delete/add 顺序，并按 _rowid 或 image_path 模拟行删除。"""

    def __init__(self, rows=None, supports_rowid=True):
        self.rows = []
        self.operations = []
        self.supports_rowid = supports_rowid
        self._next_rowid = 0
        for row in rows or []:
            self._append(row)

    def _append(self, row):
        row = dict(row)
        row.setdefault("_rowid", self._next_rowid)
        self._next_rowid = max(self._next_rowid, int(row["_rowid"]) + 1)
        self.rows.append(row)
        return row

    def delete(self, where):
        self.operations.append(("delete", where))
        if where == "true":
            removed = len(self.rows)
            self.rows = []
            return SimpleNamespace(num_deleted_rows=removed)

        match = re.match(r"^_rowid IN \((.*)\)$", where)
        if match:
            ids = {int(value) for value in match.group(1).split(",")}
            stale = [row for row in self.rows if row["_rowid"] in ids]
        else:
            match = re.match(r"^image_path IN \((.*)\)$", where)
            if not match:
                raise AssertionError(f"unexpected delete where: {where}")
            paths = {_unquote(value)
                     for value in re.findall(r"'((?:[^']|'')*)'", match.group(1))}
            stale = [row for row in self.rows if row["image_path"] in paths]

        stale_ids = {id(row) for row in stale}
        self.rows = [row for row in self.rows if id(row) not in stale_ids]
        return SimpleNamespace(num_deleted_rows=len(stale))

    def add(self, rows):
        self.operations.append(("add", [row["image_path"] for row in rows]))
        for row in rows:
            self._append(row)

    def to_lance(self):
        return _FakeLanceTable(self)

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

    def _make_files(self, tmp, *names):
        paths = []
        for name in names:
            path = os.path.join(tmp, name)
            with open(path, "wb") as fh:
                fh.write(b"not-a-real-image")
            paths.append(path)
        return paths

    def test_changed_image_added_before_rowid_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            changed, new = self._make_files(tmp, "changed.png", "new.png")
            table = FakeImageTable([
                {"image_path": changed,
                 "mtime_ns": os.stat(changed).st_mtime_ns - 1,
                 "_rowid": 7},
            ])

            output = self._run_ingest(table, tmp)

            self.assertIn("新增/更新 2 张", output)
            self.assertIn("替换旧行 1", output)
            add_index = next(i for i, op in enumerate(table.operations)
                             if op[0] == "add")
            delete_op = next(op for op in table.operations if op[0] == "delete")
            self.assertLess(add_index, table.operations.index(delete_op))
            self.assertEqual(delete_op, ("delete", "_rowid IN (7)"))
            self.assertEqual(len(table.rows), 2)
            self.assertEqual({row["image_path"] for row in table.rows}, {changed, new})
            changed_row = next(row for row in table.rows
                               if row["image_path"] == changed)
            self.assertEqual(changed_row["mtime_ns"], os.stat(changed).st_mtime_ns)

    def test_multiple_changed_rows_deleted_in_one_sorted_statement(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = self._make_files(tmp, "a.png", "b.png")
            table = FakeImageTable([
                {"image_path": second,
                 "mtime_ns": os.stat(second).st_mtime_ns - 1, "_rowid": 9},
                {"image_path": first,
                 "mtime_ns": os.stat(first).st_mtime_ns - 1, "_rowid": 3},
            ])

            self._run_ingest(table, tmp)

            deletes = [op for op in table.operations if op[0] == "delete"]
            self.assertEqual(deletes, [("delete", "_rowid IN (3,9)")])
            self.assertEqual(len(table.rows), 2)
            self.assertEqual({row["image_path"] for row in table.rows},
                             {first, second})

    def test_duplicate_rows_same_mtime_replaced_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_files(tmp, "dup.png")[0]
            mtime = os.stat(path).st_mtime_ns
            table = FakeImageTable([
                {"image_path": path, "mtime_ns": mtime, "_rowid": 4},
                {"image_path": path, "mtime_ns": mtime, "_rowid": 5},
            ])

            output = self._run_ingest(table, tmp)

            self.assertIn("新增/更新 1 张", output)
            self.assertIn("替换旧行 2", output)
            self.assertIn(("delete", "_rowid IN (4,5)"), table.operations)
            self.assertEqual(len(table.rows), 1)
            self.assertEqual(table.rows[0]["image_path"], path)
            self.assertEqual(table.rows[0]["mtime_ns"], mtime)

    def test_unchanged_image_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_files(tmp, "same.png")[0]
            table = FakeImageTable([
                {"image_path": path, "mtime_ns": os.stat(path).st_mtime_ns},
            ])

            output = self._run_ingest(table, tmp)

            self.assertIn("无新增/变更图片", output)
            self.assertEqual(table.operations, [])

    def test_missing_rowid_falls_back_to_path_delete_before_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_files(tmp, "it's.png")[0]
            table = FakeImageTable([
                {"image_path": path,
                 "mtime_ns": os.stat(path).st_mtime_ns - 1, "_rowid": 0},
            ], supports_rowid=False)

            output = self._run_ingest(table, tmp)

            escaped = path.replace("'", "''")
            expected_delete = ("delete", f"image_path IN ('{escaped}')")
            self.assertIn(expected_delete, table.operations)
            self.assertLess(table.operations.index(expected_delete),
                            next(i for i, op in enumerate(table.operations)
                                 if op[0] == "add"))
            self.assertIn("未取得 _rowid", output)
            self.assertIn("风险", output)
            self.assertEqual(len(table.rows), 1)
            self.assertEqual(table.rows[0]["mtime_ns"],
                             os.stat(path).st_mtime_ns)


class _FakeSearchQuery:
    def __init__(self):
        self.where_sql = None

    def metric(self, metric):
        return self

    def limit(self, limit):
        return self

    def where(self, where):
        self.where_sql = where
        return self

    def to_list(self):
        return []


class _FakeSearchTable:
    def __init__(self):
        self.query = _FakeSearchQuery()

    def search(self, vector, vector_column_name=None):
        return self.query


class TestSearchImagesFilter(unittest.TestCase):
    def _where_for(self, source_filter):
        table = _FakeSearchTable()
        with mock.patch.object(kb_img, "get_image_table", return_value=table), \
             mock.patch.object(kb_img, "image_count", return_value=0), \
             mock.patch.object(kb_img, "embed_query_text",
                               return_value=[0.0, 0.0]):
            kb_img.search_images_structured("猫", source_filter=source_filter)
        return table.query.where_sql

    def test_underscore_percent_quote_and_backslash_escaped(self):
        where = self._where_for(r"a_b%c'd\e")
        self.assertEqual(
            where,
            "image_path LIKE '%a\\_b\\%c''d\\\\e%' ESCAPE '\\' OR "
            "source_doc LIKE '%a\\_b\\%c''d\\\\e%' ESCAPE '\\'",
        )

    def test_underscore_case(self):
        where = self._where_for("a_b")
        self.assertEqual(
            where,
            "image_path LIKE '%a\\_b%' ESCAPE '\\' OR "
            "source_doc LIKE '%a\\_b%' ESCAPE '\\'",
        )

    def test_percent_case(self):
        where = self._where_for("50%")
        self.assertEqual(
            where,
            "image_path LIKE '%50\\%%' ESCAPE '\\' OR "
            "source_doc LIKE '%50\\%%' ESCAPE '\\'",
        )

    def test_single_quote_case(self):
        where = self._where_for("it's")
        self.assertEqual(
            where,
            "image_path LIKE '%it''s%' ESCAPE '\\' OR "
            "source_doc LIKE '%it''s%' ESCAPE '\\'",
        )


if __name__ == "__main__":
    unittest.main()
