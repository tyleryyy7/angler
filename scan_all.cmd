@echo off
cd /d %~dp0
.venv\Scripts\pythonw.exe fisher_scanner.py --once --pool-file pool_right.csv
.venv\Scripts\pythonw.exe fisher_scanner.py --once --pool-file pool_left.csv
.venv\Scripts\pythonw.exe fisher_scanner.py --once --pool-file holdings.csv --side down
