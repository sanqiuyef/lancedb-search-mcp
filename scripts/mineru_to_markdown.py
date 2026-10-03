# -*- coding: utf-8 -*-
"""批量 PDF → Markdown（MinerU pipeline 引擎，公式 → LaTeX + 图片提取）。

转换核心在 kb/pdf_convert.py（与 lancedb-search 入库管线共用同一实现）；
本脚本只负责批量调度：并行 worker、断点续跑、清单统计。

引擎：MinerU 3.4.4 pipeline（独立 venv：.mineru-venv，transformers 4.57）。
档位规则详见 kb/pdf_convert.py 模块 docstring（破损文本层/中文 → ocr，其余 auto）。

输出布局（与 lancedb-search 的 .md 入库直接兼容）：
  <out_dir>/<stem>.md
  <out_dir>/<stem>_images/img-001.jpg ...

用法：
  <anaconda python> scripts/mineru_to_markdown.py <PDF 目录> <输出目录>
      [--resume] [--workers 3] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_DIR = HERE.parent
sys.path.insert(0, str(SERVER_DIR))

from kb import config as cfg  # noqa: E402
from kb import pdf_convert  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="MinerU 批量 PDF→Markdown")
    ap.add_argument("input", help="PDF 目录或单个 PDF")
    ap.add_argument("output", help="Markdown 输出目录")
    ap.add_argument("--staging", default=cfg.MINERU_STAGING_DIR,
                    help="短路径暂存目录（规避 Windows 260 字符路径上限）")
    ap.add_argument("--mineru-bin", default=cfg.MINERU_BIN)
    ap.add_argument("--resume", action="store_true", help="跳过已存在 .md")
    ap.add_argument("--workers", type=int, default=1,
                    help="并行 worker 数（每 worker 独立模型实例，实测单实例约 1.8G 显存，8G 卡建议 ≤3）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--manifest", default="")
    args = ap.parse_args()

    # 覆盖全局配置（脚本级）
    cfg.MINERU_BIN = args.mineru_bin
    cfg.MINERU_STAGING_DIR = args.staging

    target = Path(args.input)
    pdfs = [target] if target.is_file() else sorted(target.rglob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        print("未找到 PDF")
        return 1

    out_root = Path(args.output)
    staging_root = Path(args.staging)
    if not pdf_convert.mineru_available():
        print(f"[ERROR] 找不到 MinerU CLI: {cfg.MINERU_BIN}", file=sys.stderr)
        return 1

    stats, failed, done, skipped = [], [], 0, 0
    lock = threading.Lock()

    # slot 必须按"线程"分配：任务序号 % workers 会因线程池实际调度顺序错位，
    # 导致两个并发任务共用暂存目录、互删产物（曾致 "MinerU 未产出 markdown"）。
    slot_local = threading.local()
    slot_pool = [0]

    def get_slot() -> int:
        if not hasattr(slot_local, "id"):
            with lock:
                slot_local.id = slot_pool[0]
                slot_pool[0] += 1
        return slot_local.id

    def process(pdf: Path, n: int, slot) -> bool:
        """单文件转换；slot 决定独立暂存目录。返回是否成功。"""
        nonlocal done
        stem = pdf.stem
        lang, method = pdf_convert.pick_profile(pdf)
        with lock:
            print(f"[{n}/{len(pdfs)}] w{slot} {lang}/{method} {stem[:60]}", flush=True)
        t0 = time.time()
        try:
            raw_md = pdf_convert.run_mineru(pdf, staging_root / f"w{slot}", lang, method)
            info = pdf_convert._export_markdown(raw_md, out_root, stem)
            info.update({"pdf": str(pdf), "lang": lang, "method": method,
                         "seconds": round(time.time() - t0, 1)})
            with lock:
                stats.append(info)
                done += 1
                print(f"    ✓ w{slot} {info['seconds']}s, {info['chars']} 字符, "
                      f"{info['images']} 图片, 约 {info['formulas']} 处公式", flush=True)
            return True
        except Exception as exc:
            with lock:
                failed.append({"pdf": str(pdf), "error": str(exc)[:300]})
                print(f"    ✗ w{slot} 失败: {str(exc)[:200]}", file=sys.stderr, flush=True)
            return False

    t_all = time.time()
    print(f"并行 worker 数: {max(1, args.workers)}", flush=True)

    todo = []
    for n, pdf in enumerate(pdfs, 1):
        if args.resume and (out_root / f"{pdf.stem}.md").exists():
            skipped += 1
            print(f"[{n}/{len(pdfs)}] 跳过 {pdf.stem[:60]}")
            continue
        todo.append((pdf, n))

    def job(item):
        pdf, n = item
        return item, process(pdf, n, get_slot())

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(job, todo))

    # 并发环境偶发失败（如内部线程竞态）：串行重试一次
    retry_items = [item for item, ok in results if not ok]
    if retry_items:
        print(f"\n串行重试 {len(retry_items)} 个失败文件……", flush=True)
        for pdf, n in retry_items:
            if process(pdf, n, "retry"):
                with lock:
                    failed[:] = [f for f in failed if f["pdf"] != str(pdf)]

    summary = {
        "engine": "MinerU 3.4.4 pipeline (venv, via kb.pdf_convert)",
        "input": str(target), "output": str(out_root),
        "total": len(pdfs), "done": done, "skipped": skipped,
        "failed": len(failed), "elapsed_seconds": round(time.time() - t_all, 1),
        "items": stats, "errors": failed,
    }
    if args.manifest:
        Path(args.manifest).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成：{done} 成功 / {skipped} 跳过 / {len(failed)} 失败，"
          f"用时 {(time.time() - t_all) / 60:.1f} 分钟")
    if failed:
        print("失败清单：")
        for f in failed:
            print(f"  - {Path(f['pdf']).name}: {f['error'][:120]}")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
