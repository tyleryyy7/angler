# -*- coding: utf-8 -*-
"""
盘中扫描调度器（双通道互备，跑在主环境 .venv）

每个时点由 scan_all.cmd 调用一次，依次处理 6 个池：
    pool_right/left/deep/t0/t1.csv（上穿）+ holdings.csv（下穿）

通道切换逻辑（降级链 tdx → sina → gm）：
    1. 先用通达信（xmtdx）在进程内扫描——最快、无限流；
    2. 失败率 > 50% 判定通道失效，换新浪进程内重扫；
    3. 仍失败 → gm 子进程（.venv-gm）兜底（该进程自行推送）；
    4. 三者全挂 → 推送「三通道均不可用」告警。

用法：
    .venv\\Scripts\\python.exe run_scan.py
"""

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import fisher_scanner as fs

BASE = Path(__file__).resolve().parent
GM_PYTHON = BASE / ".venv-gm" / "Scripts" / "python.exe"
FAIL_RATE_LIMIT = 0.5         # 失败率超过此值判定通道失效
POOLS = [("pool_right.csv", "up"), ("pool_left.csv", "up"), ("pool_deep.csv", "up"),
         ("pool_t0.csv", "up"), ("pool_t1.csv", "up"), ("holdings.csv", "down")]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(fs.LOG_FILE, encoding="utf-8"),
                              logging.StreamHandler(sys.stdout)])


def run_gm_scan(pool_file, side):
    """用 .venv-gm 子进程跑 gm 通道扫描（该进程自行推送）。返回进程退出码。"""
    cmd = [str(GM_PYTHON), str(BASE / "fisher_scanner.py"), "--once",
           "--pool-file", pool_file, "--source", "gm"]
    if side == "down":
        cmd += ["--side", "down"]
    r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        logging.warning("gm 通道子进程异常退出(%d): %s", r.returncode, (r.stderr or "")[-300:])
    return r.returncode


def push_alert(text):
    """直接推送一条文本告警（双通道都失效时用）。"""
    logging.warning(text)
    if fs.WECOM_WEBHOOK:
        try:
            import requests
            requests.post(fs.WECOM_WEBHOOK, timeout=10,
                          json={"msgtype": "markdown", "markdown": {"content": text}})
        except Exception as e:
            logging.warning("告警推送失败: %s", e)


def scan_with_failover(pool, pool_file, pond, tag, side):
    """降级链：tdx（通达信，进程内）→ sina（进程内）→ gm（.venv-gm 子进程）。"""
    for source in ("tdx", "sina"):
        result = fs.scan(pool, label=tag, side=side, source=source)
        total = result.attrs.get("total", 0)
        fails = result.attrs.get("fails", 0)
        if not total or fails / total <= FAIL_RATE_LIMIT:
            fs.notify(result, pond, side=side)
            return
        logging.warning("%s 通道失败率 %.0f%%（%d/%d），切换下一通道: %s",
                        source, fails / total * 100, fails, total, pool_file)
    # gm 兜底（跨环境子进程，自行推送）
    if run_gm_scan(pool_file, side) != 0:
        push_alert("**%s：三通道均不可用（tdx/sina/gm），本次扫描缺失**" % pond)


def main():
    for pool_file, side in POOLS:
        p = BASE / pool_file
        if not p.exists():
            logging.info("%s 不存在，跳过", pool_file)
            continue
        pool = pd.read_csv(p, dtype={"code": str})
        if len(pool) == 0:
            logging.info("%s 为空，跳过", pool_file)
            continue

        fake_args = SimpleNamespace(pool_file=pool_file)
        scan_with_failover(pool, pool_file, fs.pool_label(fake_args),
                           fs.pool_tag(fake_args), side)


if __name__ == "__main__":
    main()
