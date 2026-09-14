# -*- coding: utf-8 -*-
"""
盘中扫描调度器（双通道互备，跑在主环境 .venv）

每个时点由 .cmd 调用一次，默认依次处理扫描项（2026-09-14 起精简，降 sina 限流风险）：
    持仓（下穿/上穿）→ pool_deep → watchlist
    （右侧/左侧/T0/T1 池盘中扫描已暂停，恢复方法见 POOLS 注释；建池不受影响）

通道切换逻辑（降级链 tdxq → sina，2026-09-14 起，gm 兜底已退役）：
    1. 先用 tdxq（通达信客户端 TQ 接口，进程内，整池预取+本地缓存）——须客户端登录运行；
    2. 失败率 > 50% 判定通道失效（如客户端未开），换新浪进程内重扫；
    3. 两者全挂 → 推送「两通道均不可用」告警。

用法：
    .venv\\Scripts\\python.exe run_scan.py              # 全量扫描，只判完结 bar（收盘后任务）
    .venv\\Scripts\\python.exe run_scan.py --live             # 全量扫描，盘中未完结 bar 也参与判定（bar 中段任务）
    .venv\\Scripts\\python.exe run_scan.py --holdings --live     # 只扫持仓（每 15 分钟任务）
    .venv\\Scripts\\python.exe run_scan.py --hssr-report          # Fisher-ESI 台账 HSSR 周报（每周日任务）
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import fisher_scanner as fs

BASE = Path(__file__).resolve().parent
FAIL_RATE_LIMIT = 0.5         # 失败率超过此值判定通道失效
CHANNELS = ("tdxq", "sina")  # 进程内通道降级链，2026-09-14 起（gm 兜底已退役）
                             # tdxq（通达信客户端 TQ 接口，须客户端登录）作主力，sina 次备
POOLS = [("holdings.csv", "down"),        # 持仓下穿：卖出预警（最优先）
         ("holdings.csv", "up"),           # 持仓上穿：回钩/加仓提示
         ("pool_deep.csv", "up"),          # 深水池优先（用户指定）
         ("watchlist.csv", "up")]          # 观察池：盯 60 分钟上穿回钩
# 2026-09-14 起为降低 sina 限流风险，暂停 右侧/左侧/T0/T1 池盘中扫描（建池不受影响）。
# 恢复时把下面四行加回 POOLS：
#         ("pool_right.csv", "up"), ("pool_left.csv", "up"),
#         ("pool_t0.csv", "up"), ("pool_t1.csv", "up"),

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(fs.LOG_FILE, encoding="utf-8"),
                              logging.StreamHandler(sys.stdout)])


def push_alert(text):
    """直接推送一条文本告警（两通道都失效时用）。"""
    logging.warning(text)
    if fs.WECOM_WEBHOOK:
        try:
            import requests
            requests.post(fs.WECOM_WEBHOOK, timeout=10,
                          json={"msgtype": "markdown", "markdown": {"content": text}})
        except Exception as e:
            logging.warning("告警推送失败: %s", e)


def scan_with_failover(pool, pool_file, pond, tag, side, live=False):
    """降级链：tdxq（进程内）→ sina（进程内）；均不可用则推送告警。"""
    for source in CHANNELS:
        result = fs.scan(pool, label=tag, side=side, source=source, live=live)
        total = result.attrs.get("total", 0)
        fails = result.attrs.get("fails", 0)
        if not total or fails / total <= FAIL_RATE_LIMIT:
            fs.notify(result, pond, side=side)
            return result
        logging.warning("%s 通道失败率 %.0f%%（%d/%d），切换下一通道: %s",
                        source, fails / total * 100, fails, total, pool_file)
    push_alert("**%s：tdxq/sina 两通道均不可用，本次扫描缺失**" % pond)
    return None


def main():
    parser = argparse.ArgumentParser(description="盘中扫描调度器")
    parser.add_argument("--holdings", action="store_true",
                        help="只扫持仓（下穿+上穿），用于每 15 分钟持仓监控")
    parser.add_argument("--live", action="store_true",
                        help="盘中信号：未完结的 60 分钟 bar 也参与判定")
    parser.add_argument("--daily-confirm", action="store_true",
                        help="深水池日共振收盘复核（60m 日内上穿 + 当日日 K 上穿）")
    parser.add_argument("--hssr-report", action="store_true",
                        help="推送 Fisher-ESI 台账 HSSR 周报（每周日任务，须在周末保护之前）")
    args = parser.parse_args()

    if args.hssr_report:
        fs.push_text(fs.hssr_report())
        return

    if datetime.now().weekday() >= 5:
        logging.info("周末不交易，退出")
        return

    if args.daily_confirm:
        p = BASE / "pool_deep.csv"
        if not p.exists():
            logging.info("pool_deep.csv 不存在，跳过")
            return
        pool = pd.read_csv(p, dtype={"code": str})
        if len(pool) == 0:
            logging.info("pool_deep.csv 为空，跳过")
            return
        result = fs.scan_deep_resonance(pool)
        if len(result):
            fs.notify(result, "深水日共振", "up")
        return

    pools = [p for p in POOLS if p[0] == "holdings.csv"] if args.holdings else POOLS
    for pool_file, side in pools:
        p = BASE / pool_file
        if not p.exists():
            logging.info("%s 不存在，跳过", pool_file)
            continue
        pool = pd.read_csv(p, dtype={"code": str})
        if len(pool) == 0:
            logging.info("%s 为空，跳过", pool_file)
            continue

        fake_args = SimpleNamespace(pool_file=pool_file)
        result = scan_with_failover(pool, pool_file, fs.pool_label(fake_args),
                                    fs.pool_tag(fake_args), side, live=args.live)
        # Fisher-ESI v2：上穿信号分流——假性失效入待激活台账，其余完结信号进场登记
        if side == "up" and result is not None and len(result):
            fs.esi_register_pending(result)
            if args.holdings:
                fs.esi_register_entries(result)
        # 持仓 60m 下穿命中 → 对应 open 台账记正常离场（failed=否）
        if args.holdings and side == "down" and result is not None and len(result):
            fs.esi_close_on_60m_down(result)

    # Fisher-ESI v2：持仓任务每次复查待激活（R2 信号激活）
    if args.holdings:
        fs.check_pending_activation()
        # R3 失效卖出判定：只在 30m bar 收盘后窗口跑（每 15 分钟持仓任务的网格上）
        now = datetime.now()
        hm = now.strftime("%H:%M")
        if any(a <= hm <= b for a, b in fs.ESI_JUDGE_WINDOWS):
            esi_result = fs.esi_check_invalidation(now)
            if esi_result is not None and len(esi_result):
                fs.notify(esi_result, "Fisher失效", "down")


if __name__ == "__main__":
    main()
