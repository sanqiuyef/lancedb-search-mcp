# -*- coding: utf-8 -*-
"""新库重建迁移脚本：从 kb-config.json 各分区源目录重建 knowledge_v2。

流程：遍历分区 source_roots → 解析（含 OCR 链）→ 分块 → SiliconFlow 批量嵌入
→ 写入新表（每文件 checkpoint，可断点续传）→ 建向量/FTS 索引 → 输出报告。

用法（在项目根目录）：
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_from_sources.py --dry-run
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_from_sources.py --project 小论文（北松区）
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_from_sources.py            # 全部分区
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kb import config as cfg  # noqa: E402
from kb import ingest as kb_ingest  # noqa: E402
from kb import schema as kb_schema  # noqa: E402

STATE_FILENAME = "rebuild_state.json"


def load_state(state_path: str) -> dict:
    if os.path.isfile(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"files": {}}


def save_state(state_path: str, state: dict) -> None:
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, state_path)


def migrate_project_from_old_db(old_db_path: str, table, project: str) -> dict:
    """源文件缺失的分区：从旧库读 chunk 文本（source/chunk_index/category 原样保留），
    重新嵌入写入新库。返回 {"chunks": int, "sources": int}。"""
    import lancedb

    if not os.path.isdir(old_db_path):
        return {"chunks": 0, "sources": 0}
    old_db = lancedb.connect(old_db_path)
    if cfg.TABLE_NAME not in old_db.table_names():
        return {"chunks": 0, "sources": 0}
    old_table = old_db.open_table(cfg.TABLE_NAME)
    arrow = old_table.to_lance().scanner(
        columns=["text", "source", "chunk_index", "category"],
        filter=f"project = '{project.replace(chr(39), chr(39) * 2)}'",
    ).to_table()
    texts = arrow.column("text").to_pylist()
    sources = arrow.column("source").to_pylist()
    indices = arrow.column("chunk_index").to_pylist()
    categories = arrow.column("category").to_pylist()
    if not texts:
        return {"chunks": 0, "sources": 0}

    seen_sources = set()
    batch: list[dict] = []
    total = 0
    now = kb_ingest.datetime_now_iso()
    for text, source, idx, cat in zip(texts, sources, indices, categories):
        if not text or len(text) < 2:
            continue
        batch.append({
            "text": text, "source": source or "（未知来源）",
            "chunk_index": int(idx or 0), "category": cat or "",
            "project": project,
            "doc_id": kb_schema.chunk_doc_id(text, source or "", int(idx or 0)),
            "ingested_at": now,
        })
        seen_sources.add(source or "（未知来源）")
        if len(batch) >= 32:
            table.add(batch)
            total += len(batch)
            batch = []
    if batch:
        table.add(batch)
        total += len(batch)
    return {"chunks": total, "sources": len(seen_sources)}


def main() -> int:
    parser = argparse.ArgumentParser(description="从源目录重建 knowledge_v2")
    parser.add_argument("--project", default="", help="只处理指定分区（默认全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不入库不嵌入")
    parser.add_argument("--db", default="", help="覆盖目标库路径")
    parser.add_argument("--old-db", default="",
                        help="旧库路径（源文件缺失的分区从这里迁移 chunk 文本；留空禁用）")
    parser.add_argument("--limit-files", type=int, default=0, help="每个分区最多处理 N 个文件（测试用）")
    parser.add_argument("--reset-state", action="store_true", help="忽略已有 checkpoint 重新开始")
    args = parser.parse_args()

    if args.db:
        os.environ["LANCEDB_DB_PATH"] = args.db

    registry = cfg.load_registry()
    projects = cfg.list_projects()
    if args.project:
        projects = [cfg.normalize_project(args.project)] if cfg.normalize_project(args.project) else [args.project]

    db_path = cfg.resolve_db_path()
    print(f"目标库: {db_path}")
    print(f"分区: {projects}")

    if args.dry_run:
        for name in projects:
            roots = cfg.source_roots(name)
            files = [fp for root in roots for fp in kb_ingest.scan_files(root)]
            total_chars = 0
            empty = 0
            for fp in files:
                text = kb_ingest.extract_text(fp)
                if not text or len(text) < 20:
                    empty += 1
                    continue
                total_chars += len(text)
            est_chunks = total_chars // cfg.CHUNK_SIZE + len(files)
            print(f"  [{name}] 源文件 {len(files)} 个 | 无文本 {empty} 个 | "
                  f"约 {total_chars} 字符 → 预估 ~{est_chunks} chunks")
        print("（dry-run 结束，未写入任何数据）")
        return 0

    table = kb_schema.get_or_create_table()
    state_path = os.path.join(str(PROJECT_ROOT), STATE_FILENAME)
    state = load_state(state_path)
    if args.reset_state:
        state = {"files": {}}

    report = {}
    started = time.time()
    for name in projects:
        roots = cfg.source_roots(name)
        files = [(root, fp) for root in roots for fp in kb_ingest.scan_files(root)]
        if not files:
            # 源文件缺失（目录不存在或为空）：从旧库迁移该分区的 chunk 文本
            if state.get("projects", {}).get(name) == "old-db-done":
                print(f"  [{name}] 旧库迁移 checkpoint 已存在，跳过")
                report[name] = {"files": 0, "chunks": 0, "skipped": 0, "failed": []}
                continue
            print(f"  [{name}] 源目录无文件，尝试从旧库迁移 chunk 文本 ...", flush=True)
            result = migrate_project_from_old_db(args.old_db, table, name)
            if result["chunks"] == 0:
                print(f"  [{name}] 旧库中也没有该分区的数据，跳过")
                report[name] = {"files": 0, "chunks": 0, "skipped": 0, "failed": ["无源文件且旧库无数据"]}
            else:
                state.setdefault("projects", {})[name] = "old-db-done"
                save_state(state_path, state)
                print(f"  [{name}] 旧库迁移完成: {result['chunks']} chunks / {result['sources']} 文档")
                report[name] = {"files": result["sources"], "chunks": result["chunks"],
                                "skipped": 0, "failed": []}
            continue
        if args.limit_files:
            files = files[: args.limit_files]
        done_chunks = 0
        skipped = 0
        failed: list[str] = []

        # 跨文件攒批：把多个文件的 chunk 合并到一次 table.add()，
        # 嵌入函数内部再按 32 条/请求分批，避免小文件逐个触发 API。
        buffer: list[dict] = []
        buffer_counts: dict[str, int] = {}  # checkpoint key → 本批内 chunk 数
        FLUSH_CHUNKS = 600

        def flush_buffer() -> None:
            nonlocal done_chunks
            if not buffer:
                return
            last_err = None
            for attempt in range(3):
                try:
                    added = kb_ingest.add_chunks(table, buffer, name)
                    done_chunks += added
                    for key, cnt in buffer_counts.items():
                        state["files"][key] = {"status": "done", "chunks": cnt}
                    save_state(state_path, state)
                    buffer.clear()
                    buffer_counts.clear()
                    return
                except Exception as e:  # API 抖动重试
                    last_err = e
                    print(f"      批量写入失败（第 {attempt + 1} 次）: {e}", flush=True)
                    time.sleep(5 * (attempt + 1))
            for key in buffer_counts:
                failed.append(f"{os.path.basename(key.split('::', 1)[1])}: {last_err}")
            buffer.clear()
            buffer_counts.clear()

        for i, (root, fp) in enumerate(files, 1):
            key = f"{name}::{fp}"
            cached = state["files"].get(key)
            if cached and cached.get("status") == "done":
                done_chunks += cached.get("chunks", 0)
                continue
            try:
                text = kb_ingest.extract_text(fp)
                if not text or len(text) < 20:
                    skipped += 1
                    state["files"][key] = {"status": "empty"}
                    continue
                rel_path = os.path.relpath(fp, root)
                chunks = kb_ingest.chunk_text(text, rel_path)
                for ch in chunks:
                    ch["project"] = name
                    ch["doc_id"] = kb_schema.chunk_doc_id(ch["text"], ch["source"], ch["chunk_index"])
                    ch["ingested_at"] = kb_ingest.datetime_now_iso()
                buffer.extend(chunks)
                buffer_counts[key] = buffer_counts.get(key, 0) + len(chunks)
            except Exception as e:
                failed.append(f"{os.path.basename(fp)}: {e}")
                state["files"][key] = {"status": "failed", "error": str(e)[:200]}
            if len(buffer) >= FLUSH_CHUNKS:
                flush_buffer()
            save_state(state_path, state)
            if i % 50 == 0 or i == len(files):
                rate = (time.time() - started) / max(i, 1)
                print(f"  [{name}] {i}/{len(files)} 文件 | 本分区 chunks={done_chunks} "
                      f"| 失败={len(failed)} | {rate:.2f}s/文件", flush=True)
        flush_buffer()
        report[name] = {"files": len(files), "chunks": done_chunks,
                        "skipped": skipped, "failed": failed}

    print("\n===== 索引构建 =====")
    print(kb_schema.ensure_vector_index(table))
    print(kb_schema.ensure_fts_index(table))
    kb_schema.fold_new_rows(table)
    kb_schema.update_db_readme()

    print("\n===== 迁移报告 =====")
    total_chunks = 0
    for name, r in report.items():
        total_chunks += r["chunks"]
        line = f"  [{name}] 文件 {r['files']} | chunks {r['chunks']} | 无文本 {r['skipped']}"
        if r["failed"]:
            line += f" | 失败 {len(r['failed'])}"
        print(line)
        for fail in r["failed"][:5]:
            print(f"      - {fail}")
        if len(r["failed"]) > 5:
            print(f"      ... 等共 {len(r['failed'])} 个")
    print(f"\n总计 chunks: {total_chunks} | 表行数: {len(table)} | 耗时 {time.time() - started:.0f}s")
    print("旧库已删除；纯文本备份: D:\\cherry-workplace\\旧库文本备份-20261002.zip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
