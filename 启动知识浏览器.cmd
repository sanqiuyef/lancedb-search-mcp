@echo off
setlocal
cd /d "%~dp0"
if exist "dist\KnowledgeBrowser\KnowledgeBrowser.exe" (
    start "LanceDB 知识浏览器" "dist\KnowledgeBrowser\KnowledgeBrowser.exe" --config "%~dp0kb-config.json" --relations "%~dp0knowledge_graph_relations.sqlite3" --content-graph "%~dp0content_graph.sqlite3"
    exit /b
)
pythonw -X utf8 knowledge_graph_desktop.py --config "%~dp0kb-config.json" --relations "%~dp0knowledge_graph_relations.sqlite3" --content-graph "%~dp0content_graph.sqlite3"
if errorlevel 1 pause
