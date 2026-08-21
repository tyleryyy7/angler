@echo off
cd /d %~dp0
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_right.csv
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_left.csv
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_deep.csv
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_t0.csv
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file pool_t1.csv
.venv\Scripts\python.exe fisher_scanner.py --once --pool-file holdings.csv --side down
