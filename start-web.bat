@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
start "" http://127.0.0.1:8765
rem 雲端 log 跟 server 印在同一個視窗；Ctrl+C 兩個一起停
start /b "" .venv\Scripts\python.exe logs.py
.venv\Scripts\python.exe web\server.py
