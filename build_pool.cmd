@echo off
cd /d %~dp0
mkdir .build_lock 2>nul
if errorlevel 1 (
    echo BUILD POOL already running, exit.
    exit /b 1
)
.venv-gm\Scripts\python.exe build_pool_gm.py
.venv\Scripts\python.exe fisher_scanner.py --annotate-hssr
rmdir .build_lock
