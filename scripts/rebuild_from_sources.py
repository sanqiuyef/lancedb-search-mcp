# -*- coding: utf-8 -*-
"""知识库批量重建脚本：扫描 vault 内目录，准备 Markdown → 分块 → 批量嵌入。

单库不分区的扁平结构（2026-10-02 起）：直接给目录，全部文档进同一个池子。
来源规则（2026-11-15 起）：只接受 vault 内目录；source 一律为 vault 相对路径。
PDF/docx/pptx/xlsx 先经 ``kb.vault_source.prepare_markdown`` 转成 vault 内
Markdown（PDF 走 MinerU，Office 走 docling → markitdown），再按 Markdown 入库，
不再把 PDF 文本层直接写库。
流程：扫描目录 → 准备/转换来源 → 分块 → 批量嵌入 → 写表（checkpoint 断点续传）
→ 建向量/FTS 索引 → 输出报告。

用法（在项目根目录）：
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_from_sources.py --dry-run "<vault>/raw-素材"
  D:/anaconda3/python.exe -X utf8 scripts/rebuild_from_sources.py "<vault>/dir1" "<vault>/dir2"
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
from kb import vault_source  # noqa: E402

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


def _is_binary(filepath: str) -> bool:
    return Path(filepath).suffix.lower() in vault_source.BINARY_EXTENSIONS


def _reusable_generated(filepath: str) -> list[str]:
    """二进制文件已有、且由本工具为同一原文件生成的 vault Markdown。"""
    return vault_source.generated_markdown_candidates(
        filepath, kb_ingest.guess_category(filepath))


def _vault_roots_error(roots: list[str]) -> list[str]:
    return [root for root in roots if not vault_source.is_under_vault(root)]


def _print_outside_vault(roots: list[str]) -> None:
    print(
        "[ERROR] 只接受 vault 内目录；外部文件请用 add_single_document 或先导入 vault",
        file=sys.stderr,
    )
    print(f"        vault 根: {vault_source.vault_root()}", file=sys.stderr)
    for root in roots:
        print(f"        不在 vault 内: {root}", file=sys.stderr)


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
    outside = _vault_roots_error(roots)
    if outside:
        _print_outside_vault(outside)
        return 1

    db_path = cfg.resolve_db_path()
    print(f"目标库: {db_path}")
    print(f"源目录: {roots}")
    print(f"vault 根: {vault_source.vault_root()}")

    if args.dry_run:
        files = [fp for root in roots for fp in kb_ingest.scan_files(root)]
        if args.limit_files:
            files = files[: args.limit_files]
        total_chars = 0
        empty = 0
        pending_conversions = 0
        generated_skip: set[str] = set()
        reusable: dict[str, str] = {}
        for fp in files:
            if not _is_binary(fp):
                continue
            generated = _reusable_generated(fp)
            if generated:
                reusable[fp] = generated[0]
                generated_skip.add(vault_source._norm_path(generated[0]))
            else:
                pending_conversions += 1
        for fp in files:
            if _is_binary(fp):
                if fp not in reusable:
                    continue
                text = kb_ingest.extract_text(reusable[fp])
            else:
                if vault_source._norm_path(fp) in generated_skip:
                    continue
                text = kb_ingest.extract_text(fp)
            if not text or len(text) < 20:
                empty += 1
                continue
            total_chars += len(text)
        est_chunks = total_chars // cfg.CHUNK_SIZE + len(files)
        print(f"  源文件 {len(files)} 个 | 无文本 {empty} 个 | "
              f"待转换二进制 {pending_conversions} 个 | "
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

    # 先处理二进制：统一经 vault_source 转换/复用 vault 内 Markdown，
    # 后续只按生成的 md 分块，绝不直接写入 PDF 文本层。
    generated_skip: set[str] = set()
    failed_md_paths: set[str] = set()
    tasks: list[tuple[str, str, str, str]] = []  # (checkpoint key, 文本文件, source, category)

    for root, fp in files:
        if not _is_binary(fp):
            continue
        key = f"{root}::{fp}"
        cached = state["files"].get(key)
        if cached and cached.get("status") == "done":
            done_chunks += cached.get("chunks", 0)
            for old_md in _reusable_generated(fp):
                generated_skip.add(vault_source._norm_path(old_md))
            continue
        try:
            prepared = vault_source.prepare_markdown(
                fp, category_hint=kb_ingest.guess_category(fp))
            generated_skip.add(vault_source._norm_path(prepared.md_path))
            tasks.append(
                (key, prepared.md_path, prepared.source, prepared.category))
        except Exception as e:
            failed.append(f"{os.path.basename(fp)}（转换失败: {e}）")
            state["files"][key] = {"status": "failed", "error": str(e)[:200]}
            for old_md in _reusable_generated(fp):
                failed_md_paths.add(vault_source._norm_path(old_md))

    # 再处理 vault 内文本：source 取 vault 相对路径；二进制生成的 md、
    # 转换失败原文件对应的旧 md 都在同一次扫描中跳过，避免重复切片。
    for root, fp in files:
        if _is_binary(fp):
            continue
        norm_fp = vault_source._norm_path(fp)
        if norm_fp in generated_skip or norm_fp in failed_md_paths:
            continue
        key = f"{root}::{fp}"
        cached = state["files"].get(key)
        if cached and cached.get("status") == "done":
            done_chunks += cached.get("chunks", 0)
            continue
        tasks.append(
            (key, fp, vault_source.rel_to_vault(fp), kb_ingest.guess_category(fp)))

    for i, (key, text_path, source, category) in enumerate(tasks, 1):
        try:
            text, is_md = kb_ingest.extract_content(text_path)
            if not text or len(text) < 20:
                skipped += 1
                state["files"][key] = {"status": "empty"}
                continue
            chunks = kb_ingest.chunk_text(text, source, is_md, category)
            for ch in chunks:
                if category:
                    ch["category"] = category
                ch["doc_id"] = kb_schema.chunk_doc_id(ch["text"], ch["source"], ch["chunk_index"])
                ch["ingested_at"] = kb_ingest.datetime_now_iso()
            buffer.extend(chunks)
            buffer_counts[key] = buffer_counts.get(key, 0) + len(chunks)
        except Exception as e:
            failed.append(f"{os.path.basename(text_path)}: {e}")
            state["files"][key] = {"status": "failed", "error": str(e)[:200]}
        if len(buffer) >= FLUSH_CHUNKS:
            flush_buffer()
        save_state(state_path, state)
        if i % 50 == 0 or i == len(tasks):
            rate = (time.time() - started) / max(i, 1)
            print(f"  {i}/{len(tasks)} 文件 | chunks={done_chunks} "
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
