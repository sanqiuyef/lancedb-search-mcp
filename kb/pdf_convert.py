# -*- coding: utf-8 -*-
r"""PDF → Markdown 核心转换器（本地 MinerU pipeline，GPU）。

2026-10-03 起因：pdfminer 主通路对 LaTeX 论文公式产生 `(cid:N)` 占位、字母打散、
二维结构丢失（全库 18300 块中 1313 块含占位符，MSE 等公式检索 0 命中）。
本模块改用本地 MinerU（独立 venv，见 .mineru-venv）把 PDF 转为带 LaTeX 公式的
Markdown，并被两处共用：

- kb.ingest：PDF 入库时优先走本转换（带磁盘缓存），失败回退文本层抽取；
- scripts/mineru_to_markdown.py：批量转换脚本。

判定规则（实测驱动）：文本层含 U+FFFD / 私用区 / 数学字母（U+1D400-1D7FF，行内
公式斜体变量）/ `(cid:` → 强制 `-m ocr`（绕过破损文本层）；正文 CJK ≥15% → ch+ocr；
其余 en+auto（文本层可靠时保真度高于 OCR）。

Windows 260 字符路径注意：MinerU 内部路径 = TEMP + 原文件名 + `\images\<64位哈希>`，
超长文件名必失败，故一律复制为短名 p.pdf 再转换。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config as cfg

# ---------- 文本归一化 ----------

LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
    "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
}
MATH_SPAN = re.compile(r"(\$\$?)(.+?)\1", re.S)
# docling 等模型会把 MSE 识别成 "M S E"（字母间插空格）
SPACED_LETTERS = re.compile(r"(?<![A-Za-z\\])([A-Za-z])(?:\s(?=[A-Za-z](?![A-Za-z])))")
# MinerU 文本层路径处理不了的字符：U+FFFD、私用区、数学字母数字符号
BROKEN_LAYER = re.compile("[\ufffd\ue000-\uf8ff\U0001D400-\U0001D7FF]")
CJK = re.compile(r"[\u3400-\u9fff]")
IMG_LINK = re.compile(r"!\[([^\]]*)\]\((images/[^)]+)\)")


def normalize_text(text: str) -> str:
    """连字归一 + LaTeX 公式内字母打散复原。"""
    for bad, good in LIGATURES.items():
        text = text.replace(bad, good)

    def _fix_math(m: re.Match) -> str:
        delim, body = m.group(1), m.group(2)
        prev = None
        while prev != body:
            prev = body
            body = SPACED_LETTERS.sub(lambda x: x.group(1), body)
        return f"{delim}{body}{delim}"

    return MATH_SPAN.sub(_fix_math, text)


# ---------- 档位判定与执行 ----------

def mineru_available() -> bool:
    return Path(cfg.MINERU_BIN).is_file()


def pick_profile(pdf: Path) -> "tuple[str, str]":
    """按 PDF 正文采样判定 (lang, method)；详见模块 docstring。"""
    try:
        import fitz  # PyMuPDF

        sample = ""
        with fitz.open(str(pdf)) as doc:
            for i in range(doc.page_count):
                sample += doc.load_page(i).get_text()
        compact = re.sub(r"\s+", "", sample)
        if compact:
            broken = bool(BROKEN_LAYER.search(sample)) or ("(cid:" in sample)
            cjk_ratio = len(CJK.findall(compact)) / len(compact)
            lang = "ch" if cjk_ratio >= 0.15 else "en"
            return lang, ("ocr" if (broken or lang == "ch") else "auto")
    except Exception:
        pass
    return ("ch", "ocr") if CJK.search(pdf.stem) else ("en", "auto")


def run_mineru(pdf: Path, staging_root: Path, lang: str, method: str,
               timeout: int = 0) -> Path:
    """调用 MinerU CLI（短名暂存），返回产出的 markdown 路径。"""
    staging = staging_root / "work"
    tmpdir = staging_root / "tmp"
    for d in (staging, tmpdir):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    short_pdf = staging / "p.pdf"
    shutil.copy2(pdf, short_pdf)

    env = dict(os.environ)
    env.setdefault("MODELSCOPE_CACHE", r"D:\huggingface\modelscope")
    env["HF_HUB_OFFLINE"] = "0"
    env["TEMP"] = env["TMP"] = str(tmpdir)
    cmd = [
        str(cfg.MINERU_BIN), "-p", str(short_pdf), "-o", str(staging / "out"),
        "-b", "pipeline", "-m", method, "-l", lang,
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout or cfg.MINERU_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"MinerU 退出码 {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout or '')[-500:]}")
    hits = sorted((staging / "out").glob("**/p.md"))
    if not hits:
        raise RuntimeError("MinerU 未产出 markdown")
    return hits[0]


def _export_markdown(raw_md: Path, out_dir: Path, stem: str) -> dict:
    """整理 MinerU 产物为 <stem>.md + <stem>_images/，并归一化文本。"""
    md = raw_md.read_text(encoding="utf-8")
    raw_root = raw_md.parent
    img_dir = out_dir / f"{stem}_images"
    mapping: dict[str, str] = {}

    def _rewrite(m: re.Match) -> str:
        alt, rel = m.group(1), m.group(2)
        src = raw_root / rel
        if not src.is_file():
            return m.group(0)
        if rel not in mapping:
            img_dir.mkdir(parents=True, exist_ok=True)
            name = f"img-{len(mapping) + 1:03d}{src.suffix.lower()}"
            shutil.copy2(src, img_dir / name)
            mapping[rel] = f"{stem}_images/{name}"
        return f"![{alt}]({mapping[rel]})"

    md = normalize_text(IMG_LINK.sub(_rewrite, md))
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(md, encoding="utf-8")
    return {
        "md_path": str(md_path),
        "chars": len(md),
        "images": len(mapping),
        "formulas": len(re.findall(r"\$\$[^$]+\$\$|\$[^$\n]+\$", md)),
    }


def cache_key(pdf: Path) -> str:
    """按 路径+大小+mtime 生成缓存键；PDF 变更即失效。"""
    st = pdf.stat()
    raw = f"{pdf.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def cache_dir() -> Path:
    return Path(cfg.MINERU_CACHE_DIR) if cfg.MINERU_CACHE_DIR \
        else Path(cfg.resolve_db_path()) / "_pdf_md_cache"


def export_copy(md_path: Path, export_dir: Path, stem: str) -> Path:
    """把转换产物导出为标准布局（供人阅读/入库/归档）：

    <export_dir>/<stem>.md + <export_dir>/<stem>_images/img-NNN.ext
    正文中的图片链接同步改写为新目录名。
    """
    src_stem = md_path.stem
    text = md_path.read_text(encoding="utf-8")
    src_img_dir = md_path.parent / f"{src_stem}_images"
    dst_img_dir = export_dir / f"{stem}_images"
    if src_img_dir.is_dir():
        dst_img_dir.mkdir(parents=True, exist_ok=True)
        for f in src_img_dir.iterdir():
            if f.is_file():
                shutil.copy2(f, dst_img_dir / f.name)
        text = text.replace(f"{src_stem}_images/", f"{stem}_images/")
    export_dir.mkdir(parents=True, exist_ok=True)
    dest_md = export_dir / f"{stem}.md"
    dest_md.write_text(text, encoding="utf-8")
    return dest_md


def convert_pdf(pdf_path: "str | Path", use_cache: bool = True,
                export_dir: "str | Path | None" = None) -> dict:
    """PDF → Markdown（公式 LaTeX + 图片另存）。带磁盘缓存。

    export_dir 不为空时，额外把产物导出为标准布局
    `<export_dir>/<stem>.md` + `<export_dir>/<stem>_images/`（含缓存命中的情况）。

    返回 {md_path, chars, images, formulas, cached, method, lang[, exported_md]}
    失败抛异常（由调用方决定回退策略）。
    """
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise FileNotFoundError(str(pdf))
    if not mineru_available():
        raise RuntimeError(f"MinerU 不可用：{cfg.MINERU_BIN}")

    entry = None
    if use_cache:
        entry = cache_dir() / cache_key(pdf)
        md_path = entry / "doc.md"
        if md_path.is_file():
            md = md_path.read_text(encoding="utf-8")
            info = {"md_path": str(md_path), "chars": len(md),
                    "images": len(list((entry / "doc_images").glob("*")))
                    if (entry / "doc_images").is_dir() else 0,
                    "formulas": len(re.findall(r"\$\$[^$]+\$\$|\$[^$\n]+\$", md)),
                    "cached": True, "method": "cache", "lang": ""}
            if export_dir:
                info["exported_md"] = str(export_copy(md_path, Path(export_dir), pdf.stem))
            return info

    staging = Path(cfg.MINERU_STAGING_DIR) / ("kb_" + cache_key(pdf) if use_cache else "kb_run")
    lang, method = pick_profile(pdf)
    t0 = time.time()
    raw_md = run_mineru(pdf, staging, lang, method)
    out_dir = entry if entry is not None else staging / "export"
    info = _export_markdown(raw_md, out_dir, "doc" if entry is not None else pdf.stem)
    info.update({"cached": False, "method": method, "lang": lang,
                 "seconds": round(time.time() - t0, 1)})
    shutil.rmtree(staging, ignore_errors=True)
    if export_dir:
        info["exported_md"] = str(export_copy(Path(info["md_path"]), Path(export_dir), pdf.stem))
    return info
