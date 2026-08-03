# -*- coding: utf-8 -*-

import tempfile
import unittest
from pathlib import Path

import lancedb
import pyarrow as pa

from knowledge_assets import (
    ACTIVE_TABLE,
    activate_generation,
    build_generation,
    export_assets,
    list_generations,
    migrate_active_chunks,
    register_active_generation,
    restore_assets,
    verify_assets,
)


def create_legacy_table(db):
    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), 2)), pa.field("text", pa.string()),
        pa.field("source", pa.string()), pa.field("chunk_index", pa.int32()), pa.field("category", pa.string()),
    ])
    return db.create_table(ACTIVE_TABLE, data=pa.Table.from_pylist([
        {"vector": [0.1, 0.2], "text": "第一段原文", "source": "lost.pdf", "chunk_index": 0, "category": "paper"},
        {"vector": [0.3, 0.4], "text": "第二段原文", "source": "lost.pdf", "chunk_index": 1, "category": "paper"},
    ], schema=schema))


class KnowledgeAssetTests(unittest.TestCase):
    def test_rebuild_and_switch_do_not_need_source_file(self):
        with tempfile.TemporaryDirectory() as root:
            db = lancedb.connect(str(Path(root, "kb")))
            create_legacy_table(db)
            self.assertEqual(2, migrate_active_chunks(db))
            register_active_generation(db, backend="old", model="old-model", dimension=2)

            generation = build_generation(
                db, backend="new", model="new-model", dimension=3,
                embedder=lambda texts: [[float(index), 1.0, 2.0] for index, _ in enumerate(texts)],
            )
            # Building is non-destructive: the active old 2-D index remains available.
            self.assertEqual(2, len(db.open_table(ACTIVE_TABLE).to_lance().to_table().column("vector")[0].as_py()))
            activate_generation(db, generation["generation_id"])
            self.assertEqual(3, len(db.open_table(ACTIVE_TABLE).to_lance().to_table().column("vector")[0].as_py()))
            self.assertEqual(2, len(list_generations(db)))

            legacy = next(item for item in list_generations(db) if item["generation_id"] == "legacy-active")
            activate_generation(db, legacy["generation_id"])
            self.assertEqual(2, len(db.open_table(ACTIVE_TABLE).to_lance().to_table().column("vector")[0].as_py()))

    def test_asset_package_restores_text_without_original_files(self):
        with tempfile.TemporaryDirectory() as root:
            source_db = lancedb.connect(str(Path(root, "source")))
            create_legacy_table(source_db)
            package = Path(root, "assets.zip")
            result = export_assets(source_db, str(package))
            self.assertEqual(2, result["chunk_count"])

            restored_db = lancedb.connect(str(Path(root, "restored")))
            restored = restore_assets(restored_db, str(package))
            self.assertEqual(2, restored["imported"])
            self.assertEqual([], verify_assets(restored_db)["invalid_chunk_ids"])
            generation = build_generation(
                restored_db, backend="test", model="test-model", dimension=2,
                embedder=lambda texts: [[1.0, float(len(text))] for text in texts],
            )
            activate_generation(restored_db, generation["generation_id"])
            rows = restored_db.open_table(ACTIVE_TABLE).to_lance().to_table().to_pylist()
            self.assertEqual(["第一段原文", "第二段原文"], [row["text"] for row in rows])


if __name__ == "__main__":
    unittest.main()
