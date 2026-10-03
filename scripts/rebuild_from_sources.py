# -*- coding: utf-8 -*-
"""知识库批量重建脚本：扫描指定目录，解析 → 分块 → 批量嵌入 → 写入单库。

单库不分区的扁平结构（2026-10-02 起）：直接给目录，全部文档进同一个池子。
流程：扫描目录 → 解析（含 OCR 链）→ 分块 → 批量嵌入 → 写表（checkpoint 断点续传）
→ 建向量/FTS 索引 → 输出报告。

用法（在项目根目录）：
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_from_sources.py --dry-run D:\\cherry-workplace\\通用
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_from_sources.py D:\\dir1 D:\\dir2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# 必须在导入 kb（读取 cfg）之前设置：批处理进程消除闲置卸载看门狗线程与 tqdm
# 监视线程，规避 Python 3.13 + torch 的线程状态原生崩溃
# （_PyThreadState_Attach: non-NULL old thread state，实测嵌入数批后必崩）。
os.environ.setdefault("LOCAL_MODEL_IDLE_UNLOAD", "0")
os.environ.setdefault("TQDM_DISABLE", "1")

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


def main() -> int:
    parser = argparse.ArgumentParser(description="从源目录重建知识库（单库扁平结构）")
    parser.add_argument("dirs", nargs="+", help="要扫描入库的源目录（可多个）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不入库不嵌入")
    parser.add_argument("--db", default="", help="覆盖目标库路径")
    parser.add_argument("--limit-files", type=int, default=0, help="最多处理 N 个文件（测试用）")
    parser.add_argument("--reset-state", action="store_true", help="忽略已有 checkpoint 重新开始")
    args = parser.parse_args()

    if args.db:
        os.environ["LANCEDB_DB_PATH"] = args.db

    roots = [os.path.abspath(d) for d in args.dirs]
    bad = [r for r in roots if not os.path.isdir(r)]
    if bad:
        print(f"目录不存在: {bad}")
        return 1

    db_path = cfg.resolve_db_path()
    print(f"目标库: {db_path}")
    print(f"源目录: {roots}")

    if args.dry_run:
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
        print(f"  源文件 {len(files)} 个 | 无文本 {empty} 个 | "
              f"约 {total_chars} 字符 → 预估 ~{est_chunks} chunks")
        print("（dry-run 结束，未写入任何数据）")
        return 0

    table = kb_schema.get_or_create_table()
    state_path = os.path.join(str(PROJECT_ROOT), STATE_FILENAME)
    state = load_state(state_path)
    if args.reset_state:
        state = {"files": {}}

    files = [(root, fp) for root in roots for fp in kb_ingest.scan_files(root)]
    if args.limit_files:
        files = files[: args.limit_files]
    if not files:
        print("源目录中没有可处理的文件。")
        return 0

    report = {"files": 0, "chunks": 0, "skipped": 0, "failed": []}
    started = time.time()
    done_chunks = 0
    skipped = 0
    failed: list[str] = []

    # 跨文件攒批：把多个文件的 chunk 合并到一次 table.add()，
    # 嵌入函数内部再按 32 条/请求分批，避免小文件逐个触发请求。
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
                print(f"      flush {len(buffer)} chunks …", flush=True)
                added = kb_ingest.add_chunks(table, buffer)
                done_chunks += added
                for key, cnt in buffer_counts.items():
                    state["files"][key] = {"status": "done", "chunks": cnt}
                save_state(state_path, state)
                print(f"      flush 完成，累计 {done_chunks} chunks", flush=True)
                buffer.clear()
                buffer_counts.clear()
                return
            except Exception as e:  # API/嵌入抖动重试
                last_err = e
                print(f"      批量写入失败（第 {attempt + 1} 次）: {e}", flush=True)
                time.sleep(5 * (attempt + 1))
        for key in buffer_counts:
            failed.append(f"{os.path.basename(key.split('::', 1)[1])}: {last_err}")
        buffer.clear()
        buffer_counts.clear()

    for i, (root, fp) in enumerate(files, 1):
        key = f"{root}::{fp}"
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
            # 类别按完整路径推断（source 存的是相对路径，缺目录信息）
            category = kb_ingest.guess_category(os.path.join(root, rel_path))
            for ch in chunks:
                if category:
                    ch["category"] = category
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
            print(f"  {i}/{len(files)} 文件 | chunks={done_chunks} "
                  f"| 失败={len(failed)} | {rate:.2f}s/文件", flush=True)
    flush_buffer()
    report.update({"files": len(files), "chunks": done_chunks,
                   "skipped": skipped, "failed": failed})

    print("\n===== 索引构建 =====")
    removed = kb_schema.dedupe_by_doc_id(table)
    if removed:
        print(f"  去重删除 {removed} 行（中断重跑的重复行）")
    print(kb_schema.ensure_vector_index(table))
    print(kb_schema.ensure_fts_index(table))
    kb_schema.fold_new_rows(table)
    kb_schema.update_db_readme()

    print("\n===== 重建报告 =====")
    line = (f"  文件 {report['files']} | chunks {report['chunks']} | "
            f"无文本 {report['skipped']}")
    if report["failed"]:
        line += f" | 失败 {len(report['failed'])}"
    print(line)
    for fail in report["failed"][:5]:
        print(f"      - {fail}")
    if len(report["failed"]) > 5:
        print(f"      ... 等共 {len(report['failed'])} 个")
    print(f"\n总计 chunks: {report['chunks']} | 表行数: {len(table)} | 耗时 {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
