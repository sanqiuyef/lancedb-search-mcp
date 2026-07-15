# -*- coding: utf-8 -*-
"""Split oversized PDFs into deterministic MinerU API segments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def split_pdf_for_mineru(
    input_path: str,
    output_dir: str,
    max_pages: int = 200,
    max_bytes: int = 200 * 1024 * 1024,
) -> dict:
    if max_pages < 1 or max_pages > 200:
        raise ValueError("max_pages 必须在 1 到 200 之间。")
    if max_bytes < 1:
        raise ValueError("max_bytes 必须大于 0。")
    source_path = os.path.abspath(input_path)
    if not os.path.isfile(source_path) or Path(source_path).suffix.lower() != ".pdf":
        raise ValueError("输入必须是存在的 PDF 文件。")
    import fitz

    os.makedirs(output_dir, exist_ok=True)
    source_hash = hashlib.sha256()
    with open(source_path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            source_hash.update(block)
    ranges = []
    stem = Path(source_path).stem
    with fitz.open(source_path) as source:
        total_pages = source.page_count
        pending = [(start, min(start + max_pages, total_pages)) for start in range(0, total_pages, max_pages)]
        while pending:
            start, end = pending.pop(0)
            temporary = os.path.abspath(os.path.join(output_dir, f".{stem}.pages-{start + 1}-{end}.tmp.pdf"))
            with fitz.open() as part:
                part.insert_pdf(source, from_page=start, to_page=end - 1, links=True, annots=True)
                part.save(temporary, garbage=3, deflate=True)
            size_bytes = os.path.getsize(temporary)
            if size_bytes > max_bytes:
                os.remove(temporary)
                if end - start <= 1:
                    raise ValueError(f"第 {start + 1} 页单页大小仍超过 API 限制。")
                middle = start + (end - start) // 2
                pending[0:0] = [(start, middle), (middle, end)]
                continue
            ranges.append((start, end, temporary, size_bytes))

    segments = []
    for segment_index, (start, end, temporary, size_bytes) in enumerate(ranges, 1):
        filename = f"{stem}.part-{segment_index:04d}.pages-{start + 1:04d}-{end:04d}.pdf"
        destination = os.path.abspath(os.path.join(output_dir, filename))
        os.replace(temporary, destination)
        segments.append({
            "segment": segment_index,
            "path": destination,
            "page_start": start + 1,
            "page_end": end,
            "page_offset": start,
            "page_count": end - start,
            "size_bytes": size_bytes,
            "status": "pending",
        })
    manifest = {
        "version": 1,
        "source_path": source_path,
        "source_sha256": source_hash.hexdigest(),
        "total_pages": total_pages,
        "max_pages": max_pages,
        "max_bytes": max_bytes,
        "segments": segments,
    }
    manifest_path = os.path.join(output_dir, f"{stem}.mineru-split-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    manifest["manifest_path"] = os.path.abspath(manifest_path)
    return manifest


def main():
    parser = argparse.ArgumentParser(description="将超过 MinerU 限制的 PDF 按页拆分")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--max-mb", type=int, default=200)
    args = parser.parse_args()
    result = split_pdf_for_mineru(args.input, args.output, args.max_pages, args.max_mb * 1024 * 1024)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
