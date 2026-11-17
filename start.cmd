@echo off
cd /d "%~dp0"

rem Use the project venv if present, otherwise the system Python
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" main.py --slack --interval=5
