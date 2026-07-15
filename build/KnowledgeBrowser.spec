# -*- mode: python ; coding: utf-8 -*-
import os

import PySide6
from PyInstaller.utils.hooks import collect_all


project_dir = os.path.dirname(os.path.abspath(SPECPATH))
datas = [
    (os.path.join(project_dir, "kb-config.json"), "."),
    (os.path.join(os.path.dirname(PySide6.__file__), "plugins"), os.path.join("PySide6", "plugins")),
]
binaries = []
hiddenimports = []
for package in ("lance", "lancedb", "mcp"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [os.path.join(project_dir, "knowledge_graph_desktop.py")],
    pathex=[project_dir],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "tensorflow", "sentence_transformers", "openpyxl", "fitz"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KnowledgeBrowser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="KnowledgeBrowser",
)
