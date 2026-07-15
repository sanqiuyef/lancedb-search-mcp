@echo off
setlocal
cd /d "%~dp0"
if exist "dist\KnowledgeBrowser\KnowledgeBrowser.exe" (
    start "LanceDB 知识浏览器" "dist\KnowledgeBrowser\KnowledgeBrowser.exe"
    exit /b
)
if exist "dist\KnowledgeGraph\KnowledgeGraph.exe" (
    start "知识库图谱" "dist\KnowledgeGraph\KnowledgeGraph.exe"
    exit /b
)
pythonw -X utf8 knowledge_graph_desktop.py
if errorlevel 1 pause
