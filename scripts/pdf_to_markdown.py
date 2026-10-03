# -*- coding: utf-8 -*-
"""PDF → Markdown（公式转 LaTeX + 图片提取）批量转换器。

引擎：docling（本地，CUDA 加速）+ 公式增强（CodeFormulaV2）→ Markdown 中的公式为 LaTeX。
输出：<out_dir>/<相对路径>/<文件名>.md + <文件名>_images/*.png（图片按文档顺序编号）。
后处理：连字归一化（ﬁ→fi）、公式内被识别模型打散的字母复原（M S E → MSE）。

用法：
    python pdf_to_markdown.py <pdf 或目录> <输出目录> [--limit N] [--resume]

--resume 时跳过输出目录中已存在的 .md（按同名文件判断）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "0")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
    "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
}

MATH_SPAN = re.compile(r"(\$\$?)(.+?)\1", re.S)
# 单个拉丁字母 + 空格 的连续串（≥3 个字母）：docling 公式识别会把 MSE 输出成 "M S E"
SPACED_LETTERS = re.compile(r"(?<![A-Za-z\\])([A-Za-z])(?:\s(?=[A-Za-z](?![A-Za-z])))")


def normalize_text(text: str) -> str:
    """连字归一 + LaTeX 公式内字母打散复原。"""
    for bad, good in LIGATURES.items():
        text = text.replace(bad, good)

    def _fix_math(m: re.Match) -> str:
        delim, body = m.group(1), m.group(2)
        prev = None
        while prev != body:  # 反复合并，处理 "M S E" 这类链式空格
            prev = body
            body = SPACED_LETTERS.sub(lambda x: x.group(1), body)
        return f"{delim}{body}{delim}"

    return MATH_SPAN.sub(_fix_math, text)


def build_converter():
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        AcceleratorDevice,
        AcceleratorOptions,
        PdfPipelineOptions,
    )

    opts = PdfPipelineOptions()
    opts.do_formula_enrichment = True
    opts.do_table_structure = True
    opts.generate_picture_images = True
    opts.images_scale = 2.0
    opts.accelerator_options = AcceleratorOptions(
        device=AcceleratorDevice.CUDA, num_threads=8
    )
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


def convert_one(converter, pdf_path: Path, out_dir: Path) -> dict:
    """转换单个 PDF；返回统计信息。"""
    stem = pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    result = converter.convert(str(pdf_path))
    doc = result.document

    # 保存图片（按文档顺序编号）
    img_dir = out_dir / f"{stem}_images"
    saved = []
    for idx, pic in enumerate(doc.iterate_items()):
        item = pic[0] if isinstance(pic, tuple) else pic
        image = getattr(item, "image", None)
        pil = getattr(image, "pil_image", None) if image is not None else None
        if pil is None:
            continue
        img_dir.mkdir(parents=True, exist_ok=True)
        name = f"img-{len(saved) + 1:03d}.png"
        try:
            pil.save(img_dir / name)
            caption = ""
            try:
                caption = item.caption_text(doc) or ""
            except Exception:
                pass
            saved.append({"file": f"{stem}_images/{name}", "caption": caption.strip()})
        except Exception as exc:  # 图片保存失败不应中断整篇
            print(f"  [warn] 图片保存失败 {name}: {exc}", file=sys.stderr)

    # 导出 Markdown，并把 <!-- image --> 占位按顺序替换为图片引用
    md = doc.export_to_markdown()
    parts = md.split("<!-- image -->")
    if len(parts) > 1 and saved:
        rebuilt = [parts[0]]
        for i, part in enumerate(parts[1:]):
            if i < len(saved):
                cap = saved[i]["caption"]
                alt = cap if cap else saved[i]["file"].split("/")[-1]
                rebuilt.append(f"\n\n![{alt}]({saved[i]['file']})\n\n")
            else:
                rebuilt.append("\n\n[图片缺失]\n\n")
            rebuilt.append(part)
        md = "".join(rebuilt)
    md = normalize_text(md)

    md_path = out_dir / f"{stem}.md"
    md_path.write_text(md, encoding="utf-8")
    return {
        "pdf": str(pdf_path),
        "md": str(md_path),
        "chars": len(md),
        "images": len(saved),
        "formulas": md.count("$$") // 2 + len(re.findall(r"(?<!\$)\$(?!\$)", md)) // 2,
        "seconds": round(time.time() - t0, 1),
    }


def collect_pdfs(target: Path) -> list[Path]:
    if target.is_file() and target.suffix.lower() == ".pdf":
        return [target]
    return sorted(p for p in target.rglob("*.pdf"))


def main():
    ap = argparse.ArgumentParser(description="PDF → Markdown（LaTeX 公式 + 图片提取）")
    ap.add_argument("input", help="PDF 文件或目录")
    ap.add_argument("output", help="输出目录")
    ap.add_argument("--limit", type=int, default=0, help="最多转换 N 个（0=全部）")
    ap.add_argument("--resume", action="store_true", help="跳过已存在 .md 的文件")
    ap.add_argument("--manifest", default="", help="统计 manifest JSON 输出路径")
    args = ap.parse_args()

    target = Path(args.input)
    out_root = Path(args.output)
    pdfs = collect_pdfs(target)
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        print("未找到 PDF")
        return 1

    # 输入是目录时保留相对子目录结构
    rel_base = target if target.is_dir() else target.parent

    converter = build_converter()
    stats = []
    done = skipped = failed = 0
    t_all = time.time()
    for n, pdf in enumerate(pdfs, 1):
        try:
            rel = pdf.relative_to(rel_base)
        except ValueError:
            rel = Path(pdf.name)
        out_dir = out_root / rel.parent
        md_path = out_dir / f"{pdf.stem}.md"
        if args.resume and md_path.exists():
            skipped += 1
            print(f"[{n}/{len(pdfs)}] 跳过（已存在） {rel}")
            continue
        print(f"[{n}/{len(pdfs)}] 转换 {rel} ...", flush=True)
        try:
            info = convert_one(converter, pdf, out_dir)
            info["rel"] = str(rel)
            stats.append(info)
            done += 1
            print(
                f"    ✓ {info['seconds']}s, {info['chars']} 字符, "
                f"{info['images']} 图片, 约 {info['formulas']} 处公式",
                flush=True,
            )
        except Exception as exc:
            failed += 1
            print(f"    ✗ 失败: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    total_s = time.time() - t_all
    summary = {
        "engine": "docling (formula enrichment, CUDA)",
        "input": str(target),
        "output": str(out_root),
        "total": len(pdfs),
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "elapsed_seconds": round(total_s, 1),
        "items": stats,
    }
    if args.manifest:
        Path(args.manifest).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        f"\n完成：{done} 成功 / {skipped} 跳过 / {failed} 失败，"
        f"用时 {total_s / 60:.1f} 分钟"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
