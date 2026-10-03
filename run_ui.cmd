@echo off
rem LanceDB 检索面板（零插件版）启动器 —— 打开 http://127.0.0.1:8002/
rem 在 Obsidian 里用「Web Viewer」打开该地址，或另存为书签。
rem 说明：下方变量若已在环境中设置则沿用，缺省时才用作者机器路径（按需修改）。
if "%HF_HOME%"=="" set HF_HOME=D:\huggingface
if "%HF_HUB_CACHE%"=="" set HF_HUB_CACHE=%HF_HOME%\hub
if "%KMP_DUPLICATE_LIB_OK%"=="" set KMP_DUPLICATE_LIB_OK=TRUE
if "%LANCEDB_DB_PATH%"=="" set LANCEDB_DB_PATH=D:\cherry-workplace\knowledge-hub\lancedb
if "%LOCAL_MODEL_IDLE_UNLOAD%"=="" set LOCAL_MODEL_IDLE_UNLOAD=300
set PY=python
if exist D:\anaconda3\python.exe set PY=D:\anaconda3\python.exe
cd /d "%~dp0"
"%PY%" -X utf8 ui_server.py
