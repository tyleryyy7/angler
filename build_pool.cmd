@echo off
cd /d %~dp0
mkdir .build_lock 2>nul
if errorlevel 1 (
    echo BUILD POOL already running, exit.
    exit /b 1
)
.venv\Scripts\python.exe D:\tdx\PYPlugins\user\tdx_postclose_download.py
.venv\Scripts\python.exe build_pool_tdxq.py
.venv\Scripts\python.exe fisher_scanner.py --annotate-hssr --source tdxq
.venv\Scripts\python.exe run_scan.py --post-close
.venv\Scripts\python.exe build_ths_block.py
rmdir .build_lock
