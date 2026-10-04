# -*- coding: utf-8 -*-
"""PDF → Markdown → 向量知识库：一条命令走完的标准管道。

固化 2026-10-03/04 的实战流程（116 篇论文验证通过：116/116、0 失败、
9256 处 LaTeX 公式、检索实测命中公式原文）：

  阶段 1（转换）PDF → Markdown（本地 MinerU pipeline，公式 → LaTeX，图片另存）
      多 worker 并行；带磁盘缓存（路径+大小+mtime）；产物落盘到 --md-dir；
      `--resume` 时已存在同名 .md 的文件跳过。
  阶段 2（入库）Markdown → LanceDB（委托 scripts/rebuild_from_sources.py）
      标题感知分块 + 主线程预计算向量 + 按 doc_id 去重 + 向量/FTS 索引维护，
      自带 checkpoint 断点续传。

用法（项目根目录执行）：
  # 全流程：论文目录 → md 目录 → 向量库
  python -X utf8 scripts/pdf_pipeline.py "D:\\论文\\文献" --md-dir "D:\\论文\\文献_md" --workers 3

  # 只转换不入库
  python -X utf8 scripts/pdf_pipeline.py <目录> --md-dir <md目录> --no-ingest

  # 只看会做什么
  python -X utf8 scripts/pdf_pipeline.py <目录> --dry-run

注意：
  - 内容有更新的 PDF 需先用 update_document / delete_documents 处理旧源（增量入库按
    source 判重，同名不会覆盖）；
  - 阶段 1 需要 .mineru-venv（见 README「启用 PDF 公式解析」）；MinerU 不可用时
    本脚本直接报错退出（不做静默降级，避免把压平公式入进库）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kb import config as cfg  # noqa: E402
from kb import pdf_convert  # noqa: E402


def collect_pdfs(target: Path) -> list[Path]:
    if target.is_file() and target.suffix.lower() == ".pdf":
        return [target]
    return sorted(p for p in target.rglob("*.pdf"))


def phase_convert(pdfs: list[Path], md_dir: Path, workers: int, resume: bool,
                  dry_run: bool) -> dict:
    """阶段 1：PDF → Markdown 落盘。返回 {done, skipped, failed[], seconds}。"""
    todo = []
    for pdf in pdfs:
        if resume and (md_dir / f"{pdf.stem}.md").exists():
            continue
        todo.append(pdf)
    print(f"[阶段 1/2] 转换 {len(todo)}/{len(pdfs)} 个 PDF → {md_dir}"
          f"（并行 {workers}）", flush=True)
    if dry_run:
        for pdf in todo[:20]:
            print(f"  - {pdf.name}")
        if len(todo) > 20:
            print(f"  ... 等共 {len(todo)} 个")
        return {"done": 0, "skipped": len(pdfs) - len(todo), "failed": [],
                "seconds": 0.0, "dry_run": True}

    lock = threading.Lock()
    stats = {"done": 0, "failed": []}
    t_all = time.time()

    def job(pdf: Path) -> None:
        t0 = time.time()
        try:
            info = pdf_convert.convert_pdf(pdf, use_cache=True, export_dir=md_dir)
            with lock:
                stats["done"] += 1
                tag = "缓存" if info.get("cached") else f"{info.get('seconds', time.time() - t0):.0f}s"
                print(f"  ✓ [{stats['done']}/{len(todo)}] {pdf.name[:56]}"
                      f"  {tag} · {info['chars']} 字 · {info['images']} 图 · "
                      f"{info['formulas']} 公式", flush=True)
        except Exception as exc:
            with lock:
                stats["failed"].append({"pdf": str(pdf), "error": str(exc)[:300]})
                print(f"  ✗ {pdf.name[:56]}: {str(exc)[:160]}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(job, todo))
    return {"done": stats["done"], "skipped": len(pdfs) - len(todo),
            "failed": stats["failed"], "seconds": round(time.time() - t_all, 1)}


def phase_ingest(md_dir: Path, dry_run: bool) -> dict:
    """阶段 2：Markdown → 向量库（委托 rebuild_from_sources.py）。"""
    mds = sorted(md_dir.glob("*.md"))
    print(f"\n[阶段 2/2] 入库 {len(mds)} 个 Markdown → {cfg.resolve_db_path()}", flush=True)
    if dry_run:
        return {"ingested": 0, "dry_run": True}
    cmd = [sys.executable, "-X", "utf8",
           str(PROJECT_ROOT / "scripts" / "rebuild_from_sources.py"), str(md_dir)]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return {"ingested": len(mds), "returncode": proc.returncode}


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF → Markdown → 向量知识库 标准管道")
    ap.add_argument("input", help="PDF 目录或单个 PDF")
    ap.add_argument("--md-dir", default="",
                    help="Markdown 输出目录（默认：<输入目录>_md）")
    ap.add_argument("--workers", type=int, default=3,
                    help="阶段 1 并行 worker 数（实测单实例约 1.8G 显存，8G 卡建议 ≤3）")
    ap.add_argument("--resume", action="store_true", help="阶段 1 跳过已存在的 .md")
    ap.add_argument("--no-ingest", action="store_true", help="只转换，不入库")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要做的处理")
    ap.add_argument("--manifest", default="", help="汇总 JSON 输出路径")
    args = ap.parse_args()

    target = Path(args.input).resolve()
    if not target.exists():
        print(f"[ERROR] 输入不存在: {target}", file=sys.stderr)
        return 1
    md_dir = Path(args.md_dir).resolve() if args.md_dir else \
        target.with_name(target.stem + "_md" if target.is_file() else target.name + "_md")

    pdfs = collect_pdfs(target)
    if not pdfs:
        print(f"[ERROR] 未找到 PDF: {target}", file=sys.stderr)
        return 1
    if not pdf_convert.mineru_available():
        print(f"[ERROR] MinerU 不可用：{cfg.MINERU_BIN}\n"
              f"        请按 README「启用 PDF 公式解析」安装 .mineru-venv"
              f"（或用 LANCEDB_MINERU_BIN 指定路径），避免公式被降级压平。",
              file=sys.stderr)
        return 1

    t0 = time.time()
    s1 = phase_convert(pdfs, md_dir, args.workers, args.resume, args.dry_run)
    s2 = {"ingested": 0, "skipped": True}
    if not args.no_ingest:
        s2 = phase_ingest(md_dir, args.dry_run)

    summary = {
        "input": str(target), "md_dir": str(md_dir),
        "pdfs": len(pdfs),
        "convert": s1, "ingest": s2,
        "elapsed_seconds": round(time.time() - t0, 1),
        "engine": "MinerU local pipeline (kb.pdf_convert) + LanceDB (bge-m3)",
    }
    if args.manifest:
        Path(args.manifest).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n===== 管道完成 =====")
    print(f"  PDF {len(pdfs)} 个 | 新转换 {s1['done']} | 跳过 {s1['skipped']} | "
          f"失败 {len(s1['failed'])}")
    print(f"  Markdown 目录: {md_dir}")
    if not args.no_ingest:
        print(f"  入库: {'已委托 rebuild_from_sources 完成' if not args.dry_run else '(dry-run)'}")
    for f in s1["failed"][:5]:
        print(f"    ✗ {Path(f['pdf']).name}: {f['error'][:100]}")
    print(f"  总用时 {summary['elapsed_seconds'] / 60:.1f} 分钟")
    return 0 if not s1["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
